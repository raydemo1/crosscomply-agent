import json
from decimal import Decimal

import httpx
import pytest

from law_agent.llm.openai_compatible import ChatMessage
from law_agent.review.evalset.qa_diagnostics import (
    BudgetStopped,
    DiagnosticClient,
    Ledger,
    Profile,
    answer_once,
    digest,
    selection_gate,
)


def profile(provider="siliconflow"):
    return Profile("candidate", "test", "https://example.invalid/v1", "TEST_DIAGNOSTIC_KEY",
                   "8", "28", provider, "https://example.invalid/beta")


def test_budget_reserves_failed_requests_and_rejects_resampling(tmp_path):
    path = tmp_path / "ledger.jsonl"
    ledger = Ledger(path, "1")
    payload = {"max_tokens": 100, "messages": []}
    reserved = ledger.reserve("one", payload, profile())
    assert reserved > 0
    assert Ledger(path, "1").spent == reserved
    with pytest.raises(BudgetStopped, match="already attempted"):
        ledger.reserve("one", payload, profile())
    with pytest.raises(BudgetStopped, match="remaining"):
        ledger.reserve("two", {"max_tokens": 1_000_000}, profile())
    ledger.settle("one", {}, profile(), reserved)
    assert ledger.spent == reserved
    ledger.settle("one", {"prompt_tokens": 10, "completion_tokens": 20}, profile(), reserved)
    assert ledger.spent == Decimal("0.00064")


def test_payloads_isolate_provider_parameters_and_do_not_serialize_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DIAGNOSTIC_KEY", "secret-test-value")
    ledger = Ledger(tmp_path / "ledger.jsonl")
    messages = [ChatMessage(role="user", content="question")]
    for provider in ("deepseek", "siliconflow"):
        client = DiagnosticClient(profile(provider), ledger, tmp_path, provider)
        payload = client.payload(messages, [])
        assert "secret-test-value" not in json.dumps(payload)
        assert "Authorization" not in payload
        if provider == "deepseek":
            assert payload["reasoning_effort"] == "low"
            assert "enable_thinking" not in payload
        else:
            assert payload["enable_thinking"] is True
            assert "thinking" not in payload and "reasoning_effort" not in payload


def test_http_failure_is_single_attempt_with_reservation_and_no_secret(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DIAGNOSTIC_KEY", "secret-test-value")
    attempts = []
    original = httpx.AsyncClient

    def fail(request):
        attempts.append(str(request.url))
        return httpx.Response(400, json={"message": "secret-test-value"})

    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(fail), **kwargs))
    client = DiagnosticClient(profile(), Ledger(tmp_path / "ledger.jsonl"), tmp_path, "probe")
    with pytest.raises(RuntimeError, match="HTTP 400"):
        client.chat_tools([ChatMessage(role="user", content="q")], tools=[])
    assert len(attempts) == 1
    assert client.ledger.spent > 0
    assert all("secret-test-value" not in p.read_text(encoding="utf-8") for p in tmp_path.iterdir())


def mock_sse(monkeypatch, events, *, delay=0):
    import anyio

    original = httpx.AsyncClient
    requests = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for event in events:
                if delay:
                    await anyio.sleep(delay)
                yield event

    def handle(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream",
            "x-siliconcloud-trace-id": "server-trace"}, stream=Stream())

    monkeypatch.setattr("httpx.AsyncClient", lambda **kwargs: original(
        transport=httpx.MockTransport(handle), **kwargs))
    return requests


def sse_event(delta=None, finish=None, **other):
    return ("data: " + json.dumps({"choices": [{"delta": delta or {}, "finish_reason": finish}],
                                  **other}) + "\n\n").encode()


def test_stream_collects_thought_tools_usage_and_distinct_timings(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DIAGNOSTIC_KEY", "secret-test-value")
    events = [sse_event({"reasoning_content": "native thought"}),
        sse_event({"tool_calls": [{"index": 0, "id": "call1", "function": {"name": "echo", "arguments": '{"value":'}}]}),
        sse_event({"tool_calls": [{"index": 0, "function": {"arguments": '"ok"}'}}]}, "tool_calls"),
        sse_event(usage={"prompt_tokens": 10, "completion_tokens": 5}), b"data: [DONE]\n\n"]
    requests = mock_sse(monkeypatch, events)
    client = DiagnosticClient(profile(), Ledger(tmp_path / "ledger.jsonl"), tmp_path, "stream")
    result = client.chat_tools([ChatMessage(role="user", content="q")], tools=[])
    assert result["reasoning_content"] == "native thought"
    assert result["tool_calls"][0]["function"]["arguments"] == '{"value":"ok"}'
    assert json.loads(requests[0].content)["stream"] is True
    record = json.loads((tmp_path / "stream.0.response.json").read_text(encoding="utf-8"))
    assert record["server_trace_id"] == "server-trace"
    assert record["timing"]["first_reasoning_ms"] is not None
    assert record["timing"]["first_text_ms"] is None
    assert record["timing"]["headers_ms"] is not None
    assert client.ledger.entries[-1]["state"] == "settled"


@pytest.mark.parametrize("events", [
    [sse_event({"content": "partial"})],
    [sse_event({"content": "partial"}, "length"), b"data: [DONE]\n\n"],
    [sse_event({"content": "partial"}), b"data: [DONE]\n\n"],
])
def test_incomplete_stream_is_not_delivered_or_cheaply_settled(tmp_path, monkeypatch, events):
    monkeypatch.setenv("TEST_DIAGNOSTIC_KEY", "secret-test-value")
    mock_sse(monkeypatch, events)
    client = DiagnosticClient(profile(), Ledger(tmp_path / "ledger.jsonl"), tmp_path, "bad")
    with pytest.raises(RuntimeError):
        client.chat_tools([ChatMessage(role="user", content="q")], tools=[])
    assert client.ledger.entries[-1]["state"] == "reserved"


def test_total_deadline_cancels_stream_and_records_stage(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DIAGNOSTIC_KEY", "secret-test-value")
    mock_sse(monkeypatch, [sse_event({"content": "late"})], delay=.1)
    client = DiagnosticClient(profile(), Ledger(tmp_path / "ledger.jsonl"), tmp_path,
                              "deadline", total_timeout=.02)
    with pytest.raises(TimeoutError):
        client.chat_tools([ChatMessage(role="user", content="q")], tools=[])
    record = json.loads((tmp_path / "deadline.0.response.json").read_text(encoding="utf-8"))
    assert record["timing"]["error_stage"] == "total_deadline"


def test_gate_requires_complete_bound_reviews_and_no_new_material_errors():
    ids = ["a", "b"]
    rows = [{"case_id": cid, "arm": arm, "error": None, "answer": "problem text"}
            for cid in ids for arm in ("baseline", "candidate")]
    reviews = [{"case_id": r["case_id"], "arm": r["arm"], "result_sha256": digest(r),
                "target_error": r["arm"] == "baseline", "material_error": False,
                "new_material_error": False,
                "quote": "problem text", "reason": "reviewed", "evidence": "source clause"}
               for r in rows]
    assert selection_gate(rows, reviews, ids)["eligible"]
    reviews[-1]["new_material_error"] = True
    assert not selection_gate(rows, reviews, ids)["eligible"]
    reviews[-1]["new_material_error"] = False
    reviews[0]["result_sha256"] = "stale"
    assert not selection_gate(rows, reviews, ids)["eligible"]
    assert not selection_gate(rows[:-1], reviews, ids)["eligible"]


def test_gate_catches_a_different_error_even_when_both_answers_have_errors():
    ids = ["a", "b"]
    rows = [{"case_id": cid, "arm": arm, "error": None, "answer": "observed quote"}
            for cid in ids for arm in ("baseline", "candidate")]
    reviews = [{"case_id": r["case_id"], "arm": r["arm"], "result_sha256": digest(r),
                "target_error": r["arm"] == "baseline", "material_error": True,
                "new_material_error": r["arm"] == "candidate", "quote": "observed quote",
                "reason": "different obligation error", "evidence": "source"} for r in rows]
    assert selection_gate(rows, reviews, ids)["new_material_errors"] == ids
    reviews[0]["quote"] = "invented quote"
    assert selection_gate(rows, reviews, ids)["reason"] == "error_quote_not_in_answer"


@pytest.mark.parametrize("delivery_error", ["HTTP 402", "HTTP 401", "HTTP 429"])
def test_batch_error_stops_before_next_call_and_persists_stop(tmp_path, monkeypatch, delivery_error):
    from scripts import diagnose_qa

    monkeypatch.setattr(diagnose_qa, "frozen", lambda _: {"budget_cny": "30", "max_tokens": 100})
    monkeypatch.setattr(diagnose_qa, "profiles_for", lambda _: {"baseline": profile(), "candidate": profile()})
    (tmp_path / "protocol.json").write_text('{"passed":true}', encoding="utf-8")
    (tmp_path / "packets.json").write_text('{"a":{},"b":{}}', encoding="utf-8")
    calls = []

    def unavailable(client, packet):
        calls.append(packet)
        return {"error": delivery_error, "latency_ms": 1}

    monkeypatch.setattr(diagnose_qa, "answer_once", unavailable)
    with pytest.raises(RuntimeError, match="HTTP failure stops"):
        diagnose_qa.run_answers(tmp_path, "fixed")
    assert len(calls) == 1
    assert json.loads((tmp_path / "batch.stopped.json").read_text())["error"] == delivery_error
    with pytest.raises(RuntimeError, match="Batch is stopped"):
        diagnose_qa.run_answers(tmp_path, "fixed")
    assert len(calls) == 1


def test_completed_batch_retains_individual_delivery_failures_without_resampling(tmp_path, monkeypatch):
    from scripts import diagnose_qa

    monkeypatch.setattr(diagnose_qa, "frozen", lambda _: {"budget_cny": "30", "max_tokens": 100})
    monkeypatch.setattr(diagnose_qa, "profiles_for", lambda _: {"baseline": profile(), "candidate": profile()})
    (tmp_path / "protocol.json").write_text('{"passed":true}', encoding="utf-8")
    (tmp_path / "packets.json").write_text('{"a":{},"b":{}}', encoding="utf-8")
    calls = []

    def citation_failure(client, packet):
        calls.append(client.prefix)
        return {"error": "citation_failure", "latency_ms": 1}

    monkeypatch.setattr(diagnose_qa, "answer_once", citation_failure)
    diagnose_qa.run_answers(tmp_path, "fixed")
    diagnose_qa.run_answers(tmp_path, "fixed")
    assert len(calls) == 4
    assert len(list(tmp_path.glob("fixed.*.result.json"))) == 4
    assert not (tmp_path / "batch.stopped.json").exists()


def test_frozen_rubric_is_not_sent_and_source_drift_blocks_live_execution(tmp_path, monkeypatch):
    from scripts import diagnose_qa

    case = {"case_id": "test", "question": "q", "material": "original material",
            "evidence": [], "rubric": {"never_send_this": "answer oracle"}}
    packet = diagnose_qa.clean_packet(case, "unchanged system")
    serialized = json.dumps(packet, ensure_ascii=False)
    assert "answer oracle" not in serialized and "rubric" not in serialized
    assert "original material" in serialized and "unchanged system" in serialized
    values = {"cases": [case], "packets": packet, "histories": {}, "heldout": [], "profiles": {}}
    manifest = {f"{k if k != 'heldout' else 'holdout'}_sha256": digest(v) for k, v in values.items()}
    manifest["source_hashes"] = {"source": "old"}
    for name, value in {**values, "manifest": manifest}.items():
        (tmp_path / (name + ".json")).write_text(json.dumps(value), encoding="utf-8")
    monkeypatch.setattr(diagnose_qa, "source_hashes", lambda: {"source": "new"})
    with pytest.raises(RuntimeError, match="Source or corpus changed"):
        diagnose_qa.frozen(tmp_path)
    assert diagnose_qa.frozen(tmp_path, check_sources=False) == manifest


def test_explicit_resume_reuses_completed_answers_and_keeps_prior_budget(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from scripts import diagnose_qa

    config = SimpleNamespace(model="deepseek-flash", base_url="https://example.invalid",
                             beta_base_url="https://example.invalid/beta")
    monkeypatch.setattr(diagnose_qa, "require_agent_llm_config", lambda: config)
    monkeypatch.setattr(diagnose_qa, "source_hashes", lambda: {"production": "unchanged"})
    monkeypatch.setattr(diagnose_qa, "original_run", lambda _: {"state": {"evidence": []}})
    monkeypatch.setattr(diagnose_qa, "history_packet", lambda cid, _: {"case_id": cid})
    before, after = tmp_path / "before", tmp_path / "after"
    diagnose_qa.prepare(before)
    cid = diagnose_qa.ORIGINALS[0]
    messages = json.loads((before / "packets.json").read_text(encoding="utf-8"))[cid]["messages"]
    result = {"case_id": cid, "arm": "baseline", "error": None, "input_sha256": digest(messages)}
    for name, value in {
        f"fixed.{cid}.baseline.result.json": result,
        f"fixed.{cid}.baseline.0.request.json": {"messages": messages},
        f"fixed.{cid}.baseline.0.response.json": {"answer": "retained"},
        "batch.stopped.json": {"reason": "HTTP 402"},
        "protocol.json": {"passed": True},
        "protocol.0.request.json": {}, "protocol.0.response.json": {},
    }.items():
        (before / name).write_text(json.dumps(value), encoding="utf-8")
    old = Ledger(before / "ledger.jsonl")
    amount = old.reserve("previous", {"max_tokens": 100}, profile())
    diagnose_qa.prepare(after, resume_from=before)
    assert json.loads((after / f"fixed.{cid}.baseline.result.json").read_text()) == result
    assert Ledger(after / "ledger.jsonl").spent == amount
    assert diagnose_qa.frozen(after)["resume_sha256"] == digest(
        json.loads((after / "resume.json").read_text(encoding="utf-8")))
    assert not (after / "batch.stopped.json").exists()


def test_answer_keeps_native_history_and_reports_truncation_separately():
    packet = {"messages": [{"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "reasoning_content": "native history"}],
        "tools": [{"type": "function", "function": {"name": "read"}}], "citations": []}

    class Truncated:
        def payload(self, messages, tools, **kwargs):
            assert messages[1].reasoning_content == "native history"
            assert tools == packet["tools"] and kwargs["tool_choice"] == "none"
            return {}

        def request(self, payload):
            return {"choices": [{"finish_reason": "length", "message": {"content": "partial"}}]}

    result = answer_once(Truncated(), packet)
    assert result["error"] == "completion_length"
    assert result["answer"] is None
    assert packet["messages"][1]["reasoning_content"] == "native history"
