import asyncio
import json
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from law_agent.config import LLMConfig
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.llm.structured_stream import LLMStreamError, LLMStreamTimeout
from law_agent.llm.usage import usage_scope


def config(**updates):
    return replace(LLMConfig(base_url="https://example.test", beta_base_url="https://example.test/beta",
        api_key="test", model="test", timeout_seconds=2, structured_output_mode="json_object",
        reasoning_effort="low", read_timeout_seconds=2, total_timeout_seconds=3), **updates)


def packet(delta=None, finish=None, **extra):
    return ("data: " + json.dumps({"choices": [{"index": 0, "delta": delta or {},
        "finish_reason": finish}], **extra}, ensure_ascii=False) + "\n\n").encode()


COMPLETE = packet({"content": '{"status":"supported"}'}, "stop") + b"data: [DONE]\n\n"


class BytesStream(httpx.AsyncByteStream):
    def __init__(self, data):
        self.data, self.closed = data, False

    async def __aiter__(self):
        for i in range(0, len(self.data), 7):
            yield self.data[i:i + 7]

    async def aclose(self):
        self.closed = True


def mock_response(monkeypatch, data, *, content_type="text/event-stream", status=200):
    requests, streams = [], []
    original = httpx.AsyncClient

    def handle(request):
        requests.append(request)
        stream = BytesStream(data)
        streams.append(stream)
        return httpx.Response(status, headers={"content-type": content_type}, stream=stream)

    monkeypatch.setattr("law_agent.llm.structured_stream.httpx.AsyncClient",
                        lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs))
    return requests, streams


def call(cfg=None):
    return OpenAICompatibleClient(cfg or config()).chat_json(
        [ChatMessage(role="user", content="json 核验原始问题")], stream=True)


def test_internal_stream_keeps_input_and_qualification_and_accounts_without_exposing_reasoning(monkeypatch):
    usage = {"prompt_tokens": 80, "prompt_cache_hit_tokens": 60, "completion_tokens": 40,
             "completion_tokens_details": {"reasoning_tokens": 30}}
    data = (b": keep-alive\n\n" + packet({"reasoning_content": "private reasoning"})
            + packet({"content": '{"status":"'}) + packet({"content": 'supported"}'}, "stop")
            + ("data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n").encode()
            + b"data: [DONE]\n\n")
    requests, streams = mock_response(monkeypatch, data)
    records = []
    with usage_scope("semantic_grounding", records.append):
        assert call() == {"status": "supported"}
    request = json.loads(requests[0].content)
    assert request["messages"] == [{"role": "user", "content": "json 核验原始问题"}]
    assert request["response_format"] == {"type": "json_object"}
    assert request["stream"] and request["stream_options"] == {"include_usage": True}
    assert request["thinking"] == {"type": "enabled"} and request["reasoning_effort"] == "low"
    assert "tools" not in request and "temperature" not in request
    assert streams[0].closed
    row = records[0]
    assert row["phase"] == "semantic_grounding" and row["streaming"] and row["status"] == "completed"
    assert (row["cache_hit_tokens"], row["cache_miss_tokens"], row["reasoning_tokens"]) == (60, 20, 30)
    assert all(row[key] is not None for key in (
        "headers_ms", "first_byte_ms", "first_event_ms", "first_reasoning_ms", "first_token_ms", "max_data_gap_ms"))
    assert "private" not in json.dumps(row) and row["error_stage"] is None


@pytest.mark.parametrize("data", [
    packet({"content": '{}'}, "stop"),
    packet({"content": '{}'}) + b"data: [DONE]\n\n",
    packet({"content": '{}'}, "length") + b"data: [DONE]\n\n",
    packet({"content": '{}'}, "content_filter") + b"data: [DONE]\n\n",
    packet({"tool_calls": [{"index": 0}]}, "stop") + b"data: [DONE]\n\n",
    packet(error={"message": "provider failure"}) + b"data: [DONE]\n\n",
    b"data: invalid-event\n\ndata: [DONE]\n\n",
    packet({}, "stop") + packet({"content": '{}'}) + b"data: [DONE]\n\n",
])
def test_partial_truncated_or_invalid_stream_is_never_delivered_as_json(monkeypatch, data):
    _, streams = mock_response(monkeypatch, data)
    records = []
    with usage_scope("semantic_grounding", records.append), pytest.raises(LLMStreamError):
        call()
    assert streams[0].closed
    assert records[0]["status"] == "failed" and records[0]["error_stage"] == "read"


def test_complete_but_invalid_json_is_a_format_failure(monkeypatch):
    mock_response(monkeypatch, packet({"content": '{bad'}, "stop") + b"data: [DONE]\n\n")
    records = []
    with usage_scope("semantic_grounding", records.append), pytest.raises(json.JSONDecodeError):
        call()
    assert records[0]["error_stage"] == "decode_json"


@pytest.mark.parametrize("status,ctype", [(503, "application/json"), (200, "application/json")])
def test_http_errors_and_wrong_response_types_are_not_parsed_as_a_verdict(monkeypatch, status, ctype):
    mock_response(monkeypatch, b'{}', content_type=ctype, status=status)
    with pytest.raises(LLMStreamError):
        call()


@contextmanager
def server(write):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                write(self.wfile)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                disconnected.set()

        def log_message(self, *_args):
            pass

    disconnected = threading.Event()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_port}", disconnected
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=1)


def test_long_generation_continues_when_reasoning_arrives_within_idle_timeout(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")

    def write(stream):
        for _ in range(8):
            stream.write(packet({"reasoning_content": "private"}))
            stream.flush()
            time.sleep(0.05)
        stream.write(COMPLETE)
        stream.flush()

    with server(write) as (url, _):
        started = time.monotonic()
        assert call(config(base_url=url, read_timeout_seconds=0.2)) == {"status": "supported"}
        assert time.monotonic() - started > 0.4


def test_read_idle_timeout_discards_partial_content(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")

    def write(stream):
        stream.write(packet({"content": '{"status":'}))
        stream.flush()
        time.sleep(0.5)

    records = []
    with (server(write) as (url, _), usage_scope("semantic_grounding", records.append),
          pytest.raises(LLMStreamTimeout, match="ReadTimeout")):
        call(config(base_url=url, read_timeout_seconds=0.1))
    assert records[0]["error_stage"] == "read" and records[0]["first_token_ms"] is not None


def test_total_deadline_cancels_the_connection_even_with_continuous_keepalives(monkeypatch):
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")

    def write(stream):
        for _ in range(500):
            stream.write(b": keep-alive\n\n")
            stream.flush()
            time.sleep(0.02)

    records = []
    with server(write) as (url, disconnected), usage_scope("semantic_grounding", records.append):
        started = time.monotonic()
        with pytest.raises(LLMStreamTimeout, match="total timeout"):
            call(config(base_url=url, read_timeout_seconds=6, total_timeout_seconds=3))
        assert time.monotonic() - started < 4.5
        assert records[0]["first_byte_ms"] is not None, "Exercise cancellation of an active stream"
        assert disconnected.wait(0.5), f"Deadline must close the request: {records}"
    assert records[0]["error_stage"] == "total_deadline"


def test_sync_verifier_client_can_also_run_from_an_existing_event_loop(monkeypatch):
    mock_response(monkeypatch, COMPLETE)

    async def verify():
        assert call() == {"status": "supported"}

    asyncio.run(verify())
