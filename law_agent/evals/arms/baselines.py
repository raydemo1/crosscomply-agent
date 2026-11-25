"""Baseline arms: what a general model or agent does with the same inputs.

* ``direct``  — same model, one structured answer from the materials alone.
* ``web``     — same model with an open Web search tool and no citation gate,
  verifier, frozen facts or human pause: a general research agent.
* ``imported`` — answers produced elsewhere (e.g. a commercial assistant run
  by hand from ``export-prompts``), read from ``<dir>/<scenario_id>.json``.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from law_agent.config import require_llm_config
from law_agent.data.schemas import StrictModel
from law_agent.evals.arms.base import ArmContext, answer_messages, scenario_prompt
from law_agent.evals.bank.schema import Scenario
from law_agent.evals.delivery import Delivery, FreeTextAnswer, from_free_text
from law_agent.evals.runs import infrastructure_http_status
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.llm import StructuredLLMNode


def _node(name: str, model_id: str | None, output_model, client=None) -> StructuredLLMNode:
    config = require_llm_config()
    node = StructuredLLMNode(node_name=name, output_model=output_model,
                             client=client or OpenAICompatibleClient(config), max_retries=2)
    if model_id:
        node.model = model_id
    return node


def _failure(arm: str, scenario: Scenario, exc: Exception, started: float) -> Delivery:
    message = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
    return Delivery(arm=arm, scenario_id=scenario.scenario_id,
                    status="blocked" if infrastructure_http_status(message) else "failed",
                    error=message[:2000], latency_ms=int((time.perf_counter() - started) * 1000))


class DirectArm:
    name = "direct"

    def __init__(self, *, model_id: str, node: StructuredLLMNode | None = None) -> None:
        self.model_id = model_id
        self._node = node

    def config(self) -> dict[str, Any]:
        return {"model": self.model_id, "tools": []}

    def run(self, scenario: Scenario, ctx: ArmContext) -> Delivery:
        started = time.perf_counter()
        try:
            node = self._node or _node("evals_direct", self.model_id, FreeTextAnswer)
            answer = node.run(answer_messages(scenario))
        except Exception as exc:  # noqa: BLE001
            return _failure(self.name, scenario, exc, started)
        return from_free_text(self.name, scenario.scenario_id, answer,
                              latency_ms=int((time.perf_counter() - started) * 1000))


class WebStep(StrictModel):
    action: Literal["search", "answer"]
    query: str | None = Field(default=None, description="action=search 时的检索词")
    answer: FreeTextAnswer | None = Field(default=None, description="action=answer 时的最终答案")

    @model_validator(mode="after")
    def matches_action(self) -> WebStep:
        if (self.action == "search") != bool(self.query) or (self.action == "answer") != (self.answer is not None):
            raise ValueError("search 需要 query，answer 需要 answer")
        return self


WEB_EXTRA = """
你可以联网检索。每一步只输出一个 JSON：要检索时 {"action":"search","query":"..."}；
准备好后 {"action":"answer","answer":{...}}。最多检索 {max_searches} 次。"""


class WebAgentArm:
    name = "web"

    def __init__(self, *, model_id: str, max_searches: int = 5, node=None, search_client=None) -> None:
        self.model_id = model_id
        self.max_searches = max_searches
        self._node = node
        self._client = search_client

    def config(self) -> dict[str, Any]:
        return {"model": self.model_id, "tools": ["open_web_search"], "max_searches": self.max_searches}

    def run(self, scenario: Scenario, ctx: ArmContext) -> Delivery:
        from law_agent.review.web_research import build_web_search_client

        started = time.perf_counter()
        try:
            node = self._node or _node("evals_web_agent", self.model_id, WebStep)
            client = self._client or build_web_search_client()
            messages = answer_messages(scenario, WEB_EXTRA.replace("{max_searches}", str(self.max_searches)))
            searches = 0
            for _ in range(self.max_searches + 3):
                step = node.run(messages)
                if step.action == "answer":
                    break
                if searches >= self.max_searches:
                    messages.append(ChatMessage(role="user", content="检索次数已用完，请直接给出 answer。"))
                    continue
                searches += 1
                results = client.search(step.query or "", max_results=5)
                ctx.event("web_search", {"query": step.query, "urls": [r.url for r in results]})
                messages += [
                    ChatMessage(role="assistant", content=step.model_dump_json()),
                    ChatMessage(role="user", content="【检索结果】" + json.dumps(
                        [{"url": r.url, "title": r.title, "published_date": r.published_date, "text": r.text[:3000]}
                         for r in results], ensure_ascii=False)),
                ]
            else:
                raise RuntimeError("web agent did not answer within its step budget")
        except Exception as exc:  # noqa: BLE001
            return _failure(self.name, scenario, exc, started)
        return from_free_text(self.name, scenario.scenario_id, step.answer, counters={"web_searches": searches},
                              latency_ms=int((time.perf_counter() - started) * 1000))


class ImportedArm:
    """Grade answers produced outside this harness under one arm label."""

    def __init__(self, *, name: str, directory: Path) -> None:
        self.name = name
        self.directory = directory

    def config(self) -> dict[str, Any]:
        return {"imported_from": str(self.directory)}

    def run(self, scenario: Scenario, ctx: ArmContext) -> Delivery:
        path = self.directory / f"{scenario.scenario_id}.json"
        if not path.exists():
            return Delivery(arm=self.name, scenario_id=scenario.scenario_id, status="failed",
                            error="没有导入的答案")
        answer = FreeTextAnswer.model_validate_json(path.read_text(encoding="utf-8"))
        return from_free_text(self.name, scenario.scenario_id, answer)


def export_prompts(scenarios: list[Scenario], directory: Path) -> int:
    """Write one prompt per scenario for running a commercial assistant by hand."""

    directory.mkdir(parents=True, exist_ok=False)
    schema = json.dumps(FreeTextAnswer.model_json_schema(), ensure_ascii=False, indent=2)
    for scenario in scenarios:
        (directory / f"{scenario.scenario_id}.md").write_text(
            "\n\n".join([
                "请作为企业数据出境合规审查顾问回答下面的审查问题，可以自行检索资料。",
                scenario_prompt(scenario),
                "最后只输出一个符合以下 JSON Schema 的 JSON，保存为同名 .json 文件：",
                "```json\n" + schema + "\n```",
            ]), encoding="utf-8")
    return len(scenarios)
