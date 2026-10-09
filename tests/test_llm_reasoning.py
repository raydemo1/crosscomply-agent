import json

import pytest

from law_agent.config import LLMConfig
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.schemas import ReviewFacts


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
    def post(payload, url):
        calls.append((payload, url))
        return {"choices": [{"message": {"content": json.dumps({"ok": True})}}]}
    monkeypatch.setattr(instance, "_post_chat", post)
    assert instance.chat_json([ChatMessage("user", "json")], structured_output_mode="json_object") == {"ok": True}
    payload, url = calls[0]
    assert url == instance.config.base_url
    assert "tool_choice" not in payload and "max_tokens" not in payload
    assert payload["thinking"]["type"] == ("disabled" if effort == "none" else "enabled")
    if effort != "none":
        assert payload["reasoning_effort"] == effort and "temperature" not in payload


def test_forced_named_tool_rejects_thinking_before_network(monkeypatch):
    instance = client("low")
    monkeypatch.setattr(instance, "_post_chat", lambda *_: pytest.fail("unexpected network call"))
    with pytest.raises(RuntimeError, match="forced named tool_choice"):
        instance.chat_json([ChatMessage("user", "json")], output_model=ReviewFacts)
