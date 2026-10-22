"""Native tool dialogue shared by the review and Q&A execution loops."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from pydantic import Field

from law_agent.data.schemas import StrictModel
from law_agent.llm.openai_compatible import ChatMessage
from law_agent.llm.usage import usage_scope
from law_agent.review.llm import ReviewWorkflowFailed
from law_agent.review.telemetry import record_llm_call, record_retry

CONTEXT_PROMPT = """
后续工具结果或程序消息中的state为状态更新，不是完整快照：evidence按chunk_id合并替换，
evidence_sources按source_id合并替换，其他已提供字段覆盖旧值。未再次发送的材料和原文仍在对话历史中，不能据此认定不存在。
工具调用只表示请求，只有对应工具结果才表示已执行；工具错误应根据具体原因修正，不猜测执行成功。
"""


class InvalidToolCall(ValueError):
    pass


@dataclass(frozen=True)
class AgentAnswer:
    content: str


@dataclass(frozen=True)
class ToolSpec:
    fields: tuple[str, ...]
    description: str
    required: tuple[str, ...] = ()


class ToolSession(StrictModel):
    messages: list[dict[str, Any]] = Field(default_factory=list)
    pending: list[dict[str, Any]] = Field(default_factory=list)
    active_id: str | None = None
    fingerprints: dict[str, str] = Field(default_factory=dict)
    step_count: int = 0

    def context_delta(self, context: dict) -> dict:
        def changed(key, value):
            digest = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if self.fingerprints.get(key) == digest:
                return False
            self.fingerprints[key] = digest
            return True

        result = {key: value for key, value in context.items() if key != "state" and changed(key, value)}
        state = context["state"]
        updates = {key: value for key, value in state.items()
                   if key not in {"tool_session", "evidence", "evidence_sources", "steps", "result"}
                   and changed(f"state:{key}", value)}
        evidence = [hit for hit in state["evidence"] if changed(f"hit:{hit['chunk_id']}", hit)]
        sources = {key: value for key, value in state.get("evidence_sources", {}).items()
                   if changed(f"source:{key}", value)}
        if evidence:
            updates["evidence"] = evidence
        if sources:
            updates["evidence_sources"] = sources
        steps = state.get("steps", [])
        external = [s for s in steps[self.step_count:] if s["action"] == "human_input"]
        if not self.messages and steps:
            updates["steps"] = steps
        elif external:
            updates["external_inputs"] = external
        self.step_count = len(steps)
        if updates:
            result["state"] = updates
        return result

    def complete(self, observation: dict, context: dict, *, stop: bool = False):
        if not self.messages:
            return
        payload = {"observation": observation, **self.context_delta(context)}
        if self.active_id is not None:
            self.messages.append({"role": "tool", "tool_call_id": self.active_id,
                                  "content": json.dumps(payload, ensure_ascii=False)})
            self.pending.pop(0)
            self.active_id = None
        elif observation and not self.pending:
            self.messages.append({"role": "user", "content": json.dumps(payload, ensure_ascii=False)})
        if stop:
            for call in self.pending:
                self.messages.append({"role": "tool", "tool_call_id": call["id"],
                                      "content": '{"error":"运行已结束，未执行此调用"}'})
            self.pending.clear()


def _parameters(schema: dict, names: tuple[str, ...], required: tuple[str, ...]) -> dict:
    parameters = {"type": "object", "properties": {name: deepcopy(schema["properties"][name]) for name in names},
                  "required": ["summary", *required], "additionalProperties": False}
    definitions = {}

    def visit(value):
        if isinstance(value, dict):
            ref = value.get("$ref", "")
            if ref.startswith("#/$defs/"):
                name = ref.removeprefix("#/$defs/")
                if name not in definitions:
                    definitions[name] = deepcopy(schema["$defs"][name])
                    visit(definitions[name])
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(parameters)
    if definitions:
        parameters["$defs"] = definitions
    return parameters


class ToolCallingAgent:
    def __init__(self, *, client, model_id, decision_model, specs: dict[str, ToolSpec], shared=("summary",),
                 phase="investigation"):
        self.client = client
        self.model_id = model_id
        self.decision_model = decision_model
        self.specs = specs
        self.shared = shared
        self.phase = phase
        self.max_retries = 1
        schema = decision_model.model_json_schema()
        self.tools = [{"type": "function", "function": {
            "name": name, "description": spec.description,
            "parameters": _parameters(schema, (*shared, *spec.fields), spec.required),
        }} for name, spec in specs.items()]

    def messages(self, session: ToolSession, *, prompt: str) -> list[ChatMessage]:
        return [ChatMessage(role="system", content=prompt + CONTEXT_PROMPT),
                *[ChatMessage(**message) for message in session.messages]]

    def decide(self, session: ToolSession, *, prompt: str, context: dict, allow_answer=False, on_text=None):
        if not session.pending:
            update = session.context_delta(context)
            if update:
                session.messages.append({"role": "user", "content": json.dumps(update, ensure_ascii=False)})
            for attempt in range(self.max_retries + 1):
                text_started = False

                def emit(delta):
                    nonlocal text_started
                    text_started = True
                    on_text(delta)

                try:
                    record_llm_call()
                    with usage_scope(self.phase):
                        message = self.client.chat_tools(
                            self.messages(session, prompt=prompt),
                            tools=self.tools, model=self.model_id,
                            **({"on_text": emit} if on_text is not None else {}),
                        )
                    calls = message.get("tool_calls") or []
                    ids = [call["id"] for call in calls]
                    if any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
                        raise ValueError("工具调用编号无效或重复")
                    for call in calls:
                        if (call.get("type") != "function" or not isinstance(call["function"]["arguments"], str)
                                or not isinstance(call["function"]["name"], str) or not call["function"]["name"]):
                            raise ValueError("工具调用格式无效")
                    break
                except (ValueError, KeyError, OSError, RuntimeError) as exc:
                    if attempt == self.max_retries or text_started:
                        raise ReviewWorkflowFailed(failed_node="agent_tool_call", reason="llm_api_error",
                                                   message=type(exc).__name__, attempts=attempt + 1) from exc
                    record_retry()
            session.messages.append({"role": "assistant", **{k: message[k] for k in (
                "content", "reasoning_content", "tool_calls") if k in message}})
            session.pending = deepcopy(calls)
            if len(calls) > 1 and any(c["function"]["name"] in {"finish", "request_input"} for c in calls):
                for call in session.pending:
                    call["execution_error"] = "finish和request_input须单独调用，不能与其他工具同时提交"
            if not calls:
                if allow_answer and (message.get("content") or "").strip():
                    return AgentAnswer(message["content"])
                raise InvalidToolCall("请使用已注册工具；完成调查或提交报告时调用finish")
        call = session.pending[0]
        session.active_id = call["id"]
        if call.get("execution_error"):
            raise InvalidToolCall(call["execution_error"])
        name = call["function"]["name"]
        if name not in self.specs:
            raise InvalidToolCall(f"未注册工具：{name}")
        try:
            arguments = json.loads(call["function"]["arguments"])
            if not isinstance(arguments, dict) or set(arguments) - {*self.shared, *self.specs[name].fields}:
                raise ValueError("工具参数必须是该工具定义内的字段")
            return self.decision_model.model_validate_json(json.dumps({"action": name, **arguments}), strict=True)
        except ValueError as exc:
            raise InvalidToolCall(str(exc)[:2000]) from exc
