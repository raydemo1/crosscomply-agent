"""Replay frozen verifier requests with bounded Flash usage and no automatic retries."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from law_agent.config import require_llm_config
from law_agent.data.schemas import StrictModel
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.agent import AgentDecision
from law_agent.review.ids import utc_now_iso
from law_agent.review.result_builder import LLMReviewResultDraft
from law_agent.review.semantic_grounding import SemanticVerdict

MAX_CALLS = 7
PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing/"


class ReportCheck(StrictModel):
    target: str = Field(min_length=1)
    status: Literal["supported", "unsupported", "uncertain"]
    reason: str = Field(min_length=1)


class ProbeVerdict(SemanticVerdict):
    report_checks: list[ReportCheck]
    research_questions: list[str]

    @model_validator(mode="after")
    def aggregate_report_checks(self):
        unresolved = [check for check in self.report_checks if check.status != "supported"]
        if unresolved and self.status != "unsupported":
            self.status = "unsupported" if any(check.status == "unsupported" for check in unresolved) else "uncertain"
            self.conclusion_reason += "\n未获支持的报告片段：\n" + "\n".join(
                f"{check.target}: {check.reason}" for check in unresolved
            )
        return self


def report_items(draft: LLMReviewResultDraft) -> dict[str, str]:
    items = {key: value for key in ("decision_summary", "legal_path", "conclusion")
             if (value := getattr(draft, key))}
    for key in ("trigger_reasons", "recommended_actions", "risk_boundaries"):
        items.update({f"{key}:{index}": text for index, text in enumerate(getattr(draft, key)) if text})
    for index, issue in enumerate(draft.issues):
        items[f"issues:{index}:finding"] = issue.finding
        items[f"issues:{index}:recommended_action"] = issue.recommended_action
    return items


def validate_verdict_coverage(verdict: ProbeVerdict, draft: LLMReviewResultDraft) -> None:
    if sorted(check.claim_index for check in verdict.claim_checks) != list(range(len(draft.claims))):
        raise ValueError("Probe did not check every claim")
    targets = [check.target for check in verdict.report_checks]
    if len(targets) != len(set(targets)) or set(targets) != set(report_items(draft)):
        raise ValueError("Probe did not check every report item")


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def peak_cost(usage: dict) -> float | None:
    if not isinstance(usage.get("prompt_tokens"), int) or not isinstance(usage.get("completion_tokens"), int):
        return None
    prompt = usage["prompt_tokens"]
    cached = max(0, min(prompt, usage.get("prompt_cache_hit_tokens", 0)))
    return (cached * 0.006 + (prompt - cached) * 0.30 + usage["completion_tokens"] * 1.20) / 1_000_000


class ProbeClient(OpenAICompatibleClient):
    def __init__(self, config, root: Path, label: str, output_model: type[BaseModel] = ProbeVerdict) -> None:
        super().__init__(config)
        self.root, self.label = root, label
        self.output_model = output_model

    def _post_chat(self, payload: dict, base_url: str) -> dict:
        if payload["model"] != "deepseek-flash" or self.config.reasoning_effort not in {"none", "low", "high"}:
            raise ValueError("This probe only permits Flash at none/low/high effort")
        records = [json.loads(path.read_text(encoding="utf-8")) for path in self.root.glob("call_*.json")]
        if len(records) >= MAX_CALLS:
            raise RuntimeError("Probe call budget exhausted")
        if self.config.reasoning_effort != "none":
            payload.pop("temperature", None)
        path = self.root / f"call_{len(records) + 1:02d}.json"
        record = {
            "label": self.label, "started_at": utc_now_iso(), "model": payload["model"],
            "reasoning_effort": self.config.reasoning_effort, "pricing_url": PRICING_URL,
            "pricing_checked_on": "2026-10-10", "messages": payload["messages"],
            "status": "started",
        }
        write_json(path, record)
        started = time.monotonic()
        try:
            data = super()._post_chat(payload, base_url)
            choice = data["choices"][0]
            record.update({
                "provider_model": data.get("model"), "usage": data.get("usage", {}),
                "peak_usd_estimate": peak_cost(data.get("usage", {})),
                "finish_reason": choice["finish_reason"], "response": choice["message"].get("content"),
            })
            if choice["finish_reason"] != "stop":
                raise RuntimeError(f"Incomplete probe output: {choice['finish_reason']}")
            parsed = self.output_model.model_validate_json(choice["message"]["content"], strict=True)
            if isinstance(parsed, ProbeVerdict):
                context = json.loads(payload["messages"][1]["content"])
                validate_verdict_coverage(parsed, LLMReviewResultDraft.model_validate(context["draft"]))
            record.update(status="completed", output=parsed.model_dump(mode="json"))
            return data
        except Exception as exc:
            record.update(status="error", error=str(exc))
            raise
        finally:
            record.update(finished_at=utc_now_iso(), latency_seconds=round(time.monotonic() - started, 3))
            write_json(path, record)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    config = require_llm_config()
    if config.model != "deepseek-flash":
        raise ValueError("Configured model must be deepseek-flash")
    for request in json.loads(args.requests.read_text(encoding="utf-8")):
        client = ProbeClient(
            replace(config, reasoning_effort=request["reasoning_effort"], timeout_seconds=120),
            args.output_dir, request["label"],
            {"semantic": ProbeVerdict, "agent": AgentDecision}[request.get("output_type", "semantic")],
        )
        raw = client.chat_json([ChatMessage(**message) for message in request["messages"]], structured_output_mode="json_object")
        output = client.output_model.model_validate(raw, strict=True).model_dump(mode="json")
        print(json.dumps({"label": request["label"], "decision": output.get("status", output.get("action"))}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
