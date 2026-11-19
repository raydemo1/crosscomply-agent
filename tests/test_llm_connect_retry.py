import ssl
import urllib.error
import urllib.request

import httpx
import pytest

from law_agent.config import EmbeddingConfig, LLMConfig
from law_agent.llm import embeddings as embeddings_module
from law_agent.llm import openai_compatible, structured_stream
from law_agent.llm.embeddings import OpenAICompatibleEmbeddings
from law_agent.llm.openai_compatible import urlopen_with_retry
from law_agent.llm.retry_budget import MAX_DELAY_SECONDS, RetryBudget


class FakeClock:
    """sleep() advances the clock, and each failed attempt costs `attempt_cost` seconds."""

    def __init__(self, attempt_cost=0.0):
        self.now, self.attempt_cost, self.sleeps = 0.0, attempt_cost, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def fail(self):
        self.now += self.attempt_cost


def _request():
    return urllib.request.Request("https://example.test/v1/chat/completions", data=b"{}", method="POST")


def _handshake_drop():
    return urllib.error.URLError(ssl.SSLEOFError(8, "EOF occurred in violation of protocol"))


def test_budget_backs_off_exponentially_with_a_cap():
    clock = FakeClock()
    budget = RetryBudget(1000, clock=clock, rng=lambda: 1.0)
    delays = [budget.next_delay() for _ in range(7)]
    assert delays == [0.5, 1, 2, 4, 8, 8, 8]
    assert max(delays) == MAX_DELAY_SECONDS


def test_budget_is_time_not_count():
    # 快速失败（握手立即被断开）可以多试几次；慢速失败（每次 10 秒）同一预算内次数更少。
    for cost, expected_attempts in ((0.0, 8), (10.0, 3)):
        clock = FakeClock(attempt_cost=cost)
        budget = RetryBudget(30, clock=clock, rng=lambda: 1.0)
        attempts = 0
        while True:
            attempts += 1
            clock.fail()
            delay = budget.next_delay()
            if delay is None:
                break
            clock.sleep(delay)
        assert attempts == expected_attempts, (cost, attempts)
        assert clock.now <= 30 + cost


def test_last_delay_never_overshoots_the_budget():
    clock = FakeClock()
    budget = RetryBudget(3, clock=clock, rng=lambda: 1.0)
    while (delay := budget.next_delay()) is not None:
        clock.sleep(delay)
    assert clock.now == pytest.approx(3)


def test_handshake_drops_are_retried_within_the_budget(monkeypatch):
    clock, calls = FakeClock(), []

    def fake_urlopen(request, timeout):
        calls.append(timeout)
        if len(calls) < 4:
            raise _handshake_drop()
        return "response"

    monkeypatch.setattr(openai_compatible.urllib.request, "urlopen", fake_urlopen)
    assert urlopen_with_retry(_request(), 30, budget_seconds=45, sleep=clock.sleep, clock=clock) == "response"
    assert len(calls) == 4 and len(clock.sleeps) == 3


def test_a_long_outage_gives_up_once_the_budget_is_spent(monkeypatch):
    clock = FakeClock(attempt_cost=5.0)

    def fake_urlopen(request, timeout):
        clock.fail()
        raise _handshake_drop()

    monkeypatch.setattr(openai_compatible.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.URLError):
        urlopen_with_retry(_request(), 30, budget_seconds=45, sleep=clock.sleep, clock=clock)
    assert 45 <= clock.now <= 45 + 5


@pytest.mark.parametrize("error", [
    urllib.error.HTTPError("https://example.test", 429, "Too Many Requests", {}, None),
    urllib.error.URLError(TimeoutError("timed out")),
])
def test_http_errors_and_timeouts_are_not_retried(monkeypatch, error):
    calls = []

    def fake_urlopen(request, timeout):
        calls.append(1)
        raise error

    monkeypatch.setattr(openai_compatible.urllib.request, "urlopen", fake_urlopen)
    with pytest.raises(urllib.error.URLError):
        urlopen_with_retry(_request(), 30, budget_seconds=45, sleep=lambda _s: None)
    assert len(calls) == 1


def _llm_config(**overrides):
    values = dict(
        base_url="https://example.test", beta_base_url="https://example.test/beta", api_key="k",
        model="m", timeout_seconds=5, structured_output_mode="json_object", reasoning_effort="none",
        connect_retry_seconds=10,
    )
    values.update(overrides)
    return LLMConfig(**values)


def test_json_stream_retries_a_dropped_connection(monkeypatch):
    attempts = []
    body = (
        'data: {"choices":[{"index":0,"delta":{"content":"{\\"ok\\": true}"}}]}\n\n'
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request):
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("EOF occurred in violation of protocol", request=request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(structured_stream.httpx, "AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs))
    monkeypatch.setattr("law_agent.llm.retry_budget.random.random", lambda: 0.0)
    result = structured_stream.receive_json_response({"model": "m", "messages": []}, _llm_config())
    assert result == {"ok": True}
    assert len(attempts) == 3


def test_embeddings_retry_rate_limits_within_the_budget(monkeypatch):
    clock, calls = FakeClock(), []

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return b'{"data": [{"index": 0, "embedding": [0.1]}]}'

    def fake_urlopen(request, timeout):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.HTTPError("https://example.test", 429, "Too Many Requests", {}, None)
        return Response()

    monkeypatch.setattr(embeddings_module.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr("law_agent.llm.retry_budget.time.monotonic", clock)
    client = OpenAICompatibleEmbeddings(EmbeddingConfig(
        provider="openai_compatible", base_url="https://example.test", api_key="k",
        model="m", dimension=1, timeout_seconds=5, retry_seconds=30,
    ))
    client._sleep = clock.sleep
    assert client.embed_query("q") == [0.1]
    assert len(calls) == 3
