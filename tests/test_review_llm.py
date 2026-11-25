"""Tests for DeepSeek-owned review LLM nodes."""

from __future__ import annotations

import json

import pytest

from law_agent.config import LLMConfig
from law_agent.llm.openai_compatible import (
    ChatMessage,
    OpenAICompatibleClient,
    _loads_tool_arguments,
)
from law_agent.review.facts import (
    build_fact_extraction_messages,
    extract_facts_with_deepseek,
)
from law_agent.review.llm import ReviewWorkflowFailed, StructuredLLMNode
from law_agent.review.schemas import (
    ReviewFacts,
)
from law_agent.review.telemetry import current_telemetry, reset_telemetry


class FakeClient:
    def __init__(self, outputs: list[dict] | None = None, errors: list[Exception] | None = None):
        self.outputs = list(outputs or [])
        self.errors = list(errors or [])
        self.calls: list[list[ChatMessage]] = []
        self.kwargs: list[dict] = []

    def chat_json(self, messages: list[ChatMessage], **kwargs) -> dict:
        self.calls.append(messages)
        self.kwargs.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        return self.outputs.pop(0)

    def chat_tools(self, messages: list[ChatMessage], **kwargs) -> dict:
        self.calls.append(messages)
        self.kwargs.append(kwargs)
        if self.errors:
            raise self.errors.pop(0)
        return self.outputs.pop(0)


def tool_message(*calls, reasoning="private reasoning"):
    return {"role": "assistant", "content": None, "reasoning_content": reasoning, "tool_calls": [
        {"id": call_id, "type": "function", "function": {
            "name": name, "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        }} for call_id, name, arguments in calls
    ]}


def sse_stream(message, finish_reason="tool_calls"):
    """Encode an assistant message as the SSE lines a streamed tool call returns."""
    delta = {"role": "assistant"}
    if message.get("reasoning_content"):
        delta["reasoning_content"] = message["reasoning_content"]
    if message.get("content"):
        delta["content"] = message["content"]
    if message.get("tool_calls"):
        delta["tool_calls"] = [{"index": index, **call} for index, call in enumerate(message["tool_calls"])]
    events = [{"choices": [{"delta": delta}]}, {"choices": [{"delta": {}, "finish_reason": finish_reason}]}]
    return [f"data: {json.dumps(event, ensure_ascii=False)}\n".encode() for event in events] + [b"data: [DONE]\n"]


def _valid_facts_payload() -> dict:
    return {
        "business_activity": "移动 App 个性化推荐",
        "data_types": ["手机号", "定位信息"],
        "sensitive_personal_info": True,
        "cross_border_transfer": True,
        "overseas_recipient": "新加坡",
        "processing_purpose": "推荐优化",
        "legal_basis_or_consent": None,
        "industry": None,
        "regions": ["CN"],
        "missing_information": ["legal_basis_or_consent"],
    }


def test_fact_prompt_contains_json_example() -> None:
    messages = build_fact_extraction_messages("材料", "问题")
    combined = "\n".join(message.content for message in messages)

    assert "json" in combined.lower()
    assert "json_example" in combined
    assert '"business_activity"' in combined


def test_structured_node_retries_validation_failure() -> None:
    reset_telemetry()
    client = FakeClient(outputs=[{"extra": "bad"}, _valid_facts_payload()])
    node = StructuredLLMNode(
        node_name="fact_extraction",
        output_model=ReviewFacts,
        client=client,  # type: ignore[arg-type]
        max_retries=1,
        trace_id="trace_1",
    )

    facts = node.run([ChatMessage(role="user", content="json")])

    assert facts.cross_border_transfer is True
    assert len(client.calls) == 2
    assert client.kwargs[0]["output_model"] is ReviewFacts
    assert client.kwargs[0]["tool_name"] == "fact_extraction"
    assert "validation_errors=" in client.calls[1][-1].content
    telemetry = current_telemetry()
    assert telemetry.llm_call_count == 2
    assert telemetry.retry_count == 1


def test_strict_tool_argument_loader_repairs_malformed_json() -> None:
    payload = (
        '{"regions": [CN], "cross_border_transfer": true, '
        '"industry": null, "missing_information": []} trailing text'
    )

    parsed = _loads_tool_arguments(payload)

    assert parsed["regions"] == ["CN"]
    assert parsed["cross_border_transfer"] is True
    assert parsed["industry"] is None


def test_strict_tool_argument_loader_requires_json_object() -> None:
    with pytest.raises(ValueError):
        _loads_tool_arguments("[1, 2, 3]")


def test_structured_node_leaves_model_to_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    # The whole workflow shares one configured model; nodes must not pick
    # their own, so the client's config.model decides.
    monkeypatch.setenv("LAWAGENT_LLM_FACT_MODEL", "some-other-model")
    client = FakeClient(outputs=[_valid_facts_payload()])
    node = StructuredLLMNode(
        node_name="fact_extraction",
        output_model=ReviewFacts,
        client=client,  # type: ignore[arg-type]
        max_retries=0,
    )

    node.run([ChatMessage(role="user", content="json")])

    assert "model" not in client.kwargs[0]


def test_structured_node_exhaustion_returns_review_failed() -> None:
    client = FakeClient(outputs=[{"extra": "bad"}, {"still": "bad"}])
    node = StructuredLLMNode(
        node_name="fact_extraction",
        output_model=ReviewFacts,
        client=client,  # type: ignore[arg-type]
        max_retries=1,
        trace_id="trace_1",
    )

    with pytest.raises(ReviewWorkflowFailed) as exc_info:
        node.run([ChatMessage(role="user", content="json")])

    failure = exc_info.value
    assert failure.failed_node == "fact_extraction"
    assert failure.reason == "pydantic_validation_failed"
    assert failure.attempts == 2
    assert failure.to_response()["status"] == "review_failed"
    assert failure.to_response()["trace_id"] == "trace_1"


@pytest.mark.parametrize("error", [TimeoutError("read timed out"), RuntimeError("HTTP 503"),
                                  RuntimeError("stream interrupted")])
def test_transport_retry_preserves_original_messages_without_model_format_feedback(error):
    messages = [ChatMessage(role="system", content="json 核验规则"),
                ChatMessage(role="user", content="原始事实和法源")]
    client = FakeClient(outputs=[_valid_facts_payload()], errors=[error])
    node = StructuredLLMNode(node_name="semantic_grounding", output_model=ReviewFacts,
                             client=client, max_retries=1, stream=True)
    assert node.run(messages).cross_border_transfer is True
    assert client.calls == [messages, messages]
    assert client.kwargs[0]["stream"] is True and client.kwargs[1]["stream"] is True


def test_json_decode_failure_retains_targeted_format_feedback():
    client = FakeClient(outputs=[_valid_facts_payload()], errors=[json.JSONDecodeError("invalid", "{bad", 1)])
    node = StructuredLLMNode(node_name="semantic_grounding", output_model=ReviewFacts,
                             client=client, max_retries=1)
    messages = [ChatMessage(role="user", content="原文")]
    node.run(messages)
    assert len(messages) == 1
    assert len(client.calls[1]) == 2
    assert "上一轮 JSON 解析失败" in client.calls[1][-1].content


def test_transport_retry_exhaustion_does_not_change_attempt_limits():
    client = FakeClient(errors=[TimeoutError("read"), TimeoutError("read")])
    node = StructuredLLMNode(node_name="semantic_grounding", output_model=ReviewFacts,
                             client=client, max_retries=1)
    with pytest.raises(ReviewWorkflowFailed) as failure:
        node.run([ChatMessage(role="user", content="原文")])
    assert failure.value.attempts == 2 and failure.value.reason == "llm_api_error"
    assert client.calls[0] == client.calls[1]


def test_strict_tool_json_errors_remain_format_failures_when_retried(monkeypatch):
    client = OpenAICompatibleClient(LLMConfig(base_url="https://example.test", beta_base_url="https://example.test/beta",
        api_key="test", model="test", timeout_seconds=10, structured_output_mode="strict_tool", reasoning_effort="none"))
    arguments = iter(["[]", json.dumps(_valid_facts_payload())])
    requests = []

    def respond(payload, _on_text):
        requests.append(payload)
        return {"role": "assistant", "content": None, "tool_calls": [{"function": {"arguments": next(arguments)}}]}

    monkeypatch.setattr(client, "_stream_tools", respond)
    node = StructuredLLMNode(node_name="fact_extraction", output_model=ReviewFacts, client=client, max_retries=1)
    assert node.run([ChatMessage(role="user", content="原文")]).cross_border_transfer is True
    assert "上一轮 JSON 解析失败" in requests[1]["messages"][-1]["content"]


def test_fact_extraction_requires_all_llm_fields() -> None:
    payload = _valid_facts_payload()
    payload.pop("missing_information")
    client = FakeClient(outputs=[payload])

    with pytest.raises(ReviewWorkflowFailed) as exc_info:
        extract_facts_with_deepseek(
            "手机号发送给新加坡",
            "是否需要数据出境安全评估？",
            client=client,  # type: ignore[arg-type]
            max_retries=0,
        )

    assert exc_info.value.reason == "pydantic_validation_failed"


def client(effort):
    return OpenAICompatibleClient(LLMConfig(
        base_url="https://example.test", beta_base_url="https://example.test/beta",
        api_key="fake", model="deepseek-flash", timeout_seconds=60,
        structured_output_mode="strict_tool", reasoning_effort=effort,
    ))


@pytest.mark.parametrize("effort", ["none", "low", "high", "max"])
def test_json_reasoning_payload_has_no_forced_tool_or_token_cap(monkeypatch, effort):
    instance = client(effort)
    calls = []
    def receive(payload, cfg, **_kwargs):
        calls.append((payload, cfg.base_url))
        return {"ok": True}
    monkeypatch.setattr("law_agent.llm.openai_compatible.receive_json_response", receive)
    assert instance.chat_json([ChatMessage("user", "json")], structured_output_mode="json_object") == {"ok": True}
    payload, url = calls[0]
    assert url == instance.config.base_url
    assert "tool_choice" not in payload and "max_tokens" not in payload
    assert payload["thinking"]["type"] == ("disabled" if effort == "none" else "enabled")
    if effort != "none":
        assert payload["reasoning_effort"] == effort and "temperature" not in payload


def test_forced_named_tool_rejects_thinking_before_network(monkeypatch):
    instance = client("low")
    monkeypatch.setattr(instance, "_stream_tools", lambda *_: pytest.fail("unexpected network call"))
    with pytest.raises(RuntimeError, match="forced named tool_choice"):
        instance.chat_json([ChatMessage("user", "json")], output_model=ReviewFacts)
