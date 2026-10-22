import json
from io import BytesIO
from urllib.error import HTTPError

import pytest

from law_agent.config import LLMConfig
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.llm.usage import usage_cost, usage_scope


def client():
    return OpenAICompatibleClient(LLMConfig(api_key="test", base_url="https://example.test", beta_base_url="https://example.test/beta",
        model="test", timeout_seconds=10, structured_output_mode="json_object", reasoning_effort="low"))


def packets(*events, done=True):
    return ("".join("data: " + json.dumps(e) + "\n\n" for e in events) + ("data: [DONE]\n\n" if done else "")).encode()


@pytest.mark.parametrize("usage_only", [True, False])
def test_stream_captures_terminal_usage_without_exposing_reasoning(monkeypatch, usage_only):
    requests, records = [], []
    usage = {"prompt_tokens": 100, "completion_tokens": 30, "prompt_cache_hit_tokens": 80,
             "completion_tokens_details": {"reasoning_tokens": 20}}
    terminal = {"usage": usage, "choices": [] if usage_only else [{"delta": {}, "finish_reason": "stop"}]}
    data = packets({"choices": [{"delta": {"reasoning_content": "private"}}]},
                   {"choices": [{"delta": {"content": "正文"}}]}, terminal)

    def respond(request, **kwargs):
        requests.append(json.loads(request.data))
        return BytesIO(data)

    monkeypatch.setattr("urllib.request.urlopen", respond)
    with usage_scope("qa_answer", records.append):
        assert list(client().stream_text([ChatMessage(role="user", content="private_input")])) == ["正文"]
    assert requests[0]["stream_options"] == {"include_usage": True}
    assert len(records) == 1
    record = records[0]
    assert record["phase"] == "qa_answer" and record["status"] == "completed"
    assert (record["prompt_tokens"], record["cache_hit_tokens"], record["cache_miss_tokens"],
            record["completion_tokens"], record["reasoning_tokens"]) == (100, 80, 20, 30, 20)
    assert record["first_token_ms"] is not None
    assert "private" not in json.dumps(record)


def test_missing_usage_is_missing_and_not_free(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: BytesIO(packets(
        {"choices": [{"delta": {"content": "正文"}, "finish_reason": "stop"}]})))
    records = []
    with usage_scope("qa_answer", records.append):
        list(client().stream_text([ChatMessage(role="user", content="问题")]))
    assert not records[0]["usage_available"]
    assert records[0]["prompt_tokens"] is None and records[0]["completion_tokens"] is None
    cost = usage_cost(records, cache_hit_per_million="1", cache_miss_per_million="2", output_per_million="3",
                      currency="USD", price_version="fixture", model="test")
    assert cost["amount"] is None and cost["missing_usage_requests"] == 1


@pytest.mark.parametrize("mode", ["truncated", "cancelled", "disconnected"])
def test_stream_failure_and_cancellation_are_accounted(monkeypatch, mode):
    events = [{"choices": [{"delta": {"content": "部分正文"}, "finish_reason": "length" if mode == "truncated" else None}]}]
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: BytesIO(packets(*events, done=False)))
    records = []
    with usage_scope("qa_answer", records.append):
        stream = client().stream_text([ChatMessage(role="user", content="问题")])
        assert next(stream) == "部分正文"
        if mode == "cancelled":
            stream.close()
        else:
            with pytest.raises(RuntimeError):
                list(stream)
    assert len(records) == 1
    assert records[0]["status"] == ("cancelled" if mode == "cancelled" else "failed")
    assert not records[0]["usage_available"]


def test_nonstream_usage_scope_is_inherited_and_restored(monkeypatch):
    response = {"choices": [{"message": {"content": "{}"}}], "usage": {"prompt_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 60}, "completion_tokens": 20}}
    monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: BytesIO(json.dumps(response).encode()))
    records = []
    with usage_scope("outer", records.append):
        with usage_scope("qa_eval_judge"):
            client().chat_json([ChatMessage(role="user", content="问题")])
        client().chat_json([ChatMessage(role="user", content="问题")])
    assert [r["phase"] for r in records] == ["qa_eval_judge", "outer"]
    assert all(r["cache_miss_tokens"] == 40 for r in records)
    assert records[0]["message_hashes"] == records[1]["message_hashes"]


def test_http_failure_is_recorded_without_assuming_zero_tokens(monkeypatch):
    def fail(*_a, **_k):
        raise HTTPError("https://example.test", 503, "unavailable", {}, BytesIO(b"unavailable"))
    monkeypatch.setattr("urllib.request.urlopen", fail)
    records = []
    with usage_scope("investigation", records.append), pytest.raises(RuntimeError):
        client().chat_json([ChatMessage(role="user", content="问题")])
    assert records[0]["status"] == "failed" and records[0]["error"] == "HTTPError"
    assert records[0]["prompt_tokens"] is None


def test_explicit_price_snapshot_does_not_charge_reasoning_twice():
    records = [{"model": "test", "cache_hit_tokens": 1_000_000, "cache_miss_tokens": 2_000_000,
                "completion_tokens": 3_000_000, "reasoning_tokens": 1_000_000}]
    rates = {"cache_hit_per_million": "0.003", "cache_miss_per_million": "0.15", "output_per_million": "0.6",
             "currency": "USD", "price_version": "fixture", "model": "test"}
    assert usage_cost(records, **rates)["amount"] == "2.103"
    with pytest.raises(ValueError, match="model"):
        usage_cost([{**records[0], "model": "other"}], **rates)
