"""Request usage accounting without retaining model input or private reasoning."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from decimal import Decimal

_phase = ContextVar("llm_usage_phase", default="unspecified")
_sink: ContextVar[Callable[[dict], None] | None] = ContextVar("llm_usage_sink", default=None)


@contextmanager
def usage_scope(phase: str, sink: Callable[[dict], None] | None = None) -> Iterator[None]:
    phase_token = _phase.set(phase)
    sink_token = _sink.set(sink) if sink is not None else None
    try:
        yield
    finally:
        _phase.reset(phase_token)
        if sink_token is not None:
            _sink.reset(sink_token)


def _count(value):
    return value if type(value) is int and value >= 0 else None


class UsageRequest:
    def __init__(self, payload: dict):
        self.started = time.monotonic()
        self.started_at = datetime.now(UTC).isoformat()
        self.phase = _phase.get()
        self.sink = _sink.get()
        self.model = payload["model"]
        self.streaming = bool(payload.get("stream"))
        self.usage = None
        self.response_model = None
        self.first_token_ms = None
        self.headers_ms = None
        self.first_byte_ms = None
        self.first_event_ms = None
        self.first_reasoning_ms = None
        self.stage = None
        self._last_data_at = None
        self._max_data_gap_ms = None
        messages = [json.dumps(m, ensure_ascii=False, separators=(",", ":")) for m in payload["messages"]]
        self.message_hashes = [hashlib.sha256(m.encode()).hexdigest() for m in messages]
        self.message_characters = [len(m) for m in messages]
        self.tools_hash = hashlib.sha256(json.dumps(payload.get("tools", []), ensure_ascii=False,
                                                  separators=(",", ":")).encode()).hexdigest()

    def observe(self, response: dict):
        if isinstance(response.get("usage"), dict):
            self.usage = response["usage"]
        if response.get("model"):
            self.response_model = response["model"]

    def token(self):
        if self.first_token_ms is None:
            self.first_token_ms = round((time.monotonic() - self.started) * 1000)

    def headers(self):
        self.headers_ms = round((time.monotonic() - self.started) * 1000)
        self.stage = "read"
        self._last_data_at = time.monotonic()

    def data(self):
        now = time.monotonic()
        if self.first_byte_ms is None:
            self.first_byte_ms = round((now - self.started) * 1000)
        gap = (now - (self._last_data_at or self.started)) * 1000
        self._max_data_gap_ms = max(self._max_data_gap_ms or 0, gap)
        self._last_data_at = now

    def event(self):
        if self.first_event_ms is None:
            self.first_event_ms = round((time.monotonic() - self.started) * 1000)

    def reasoning(self):
        if self.first_reasoning_ms is None:
            self.first_reasoning_ms = round((time.monotonic() - self.started) * 1000)

    def finish(self, status: str, error: str | None = None):
        if self._last_data_at is not None:
            self._max_data_gap_ms = max(self._max_data_gap_ms or 0,
                                       (time.monotonic() - self._last_data_at) * 1000)
        usage = self.usage or {}
        prompt = _count(usage.get("prompt_tokens"))
        completion = _count(usage.get("completion_tokens"))
        hit = _count(usage.get("prompt_cache_hit_tokens"))
        if hit is None:
            hit = _count((usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
        miss = _count(usage.get("prompt_cache_miss_tokens"))
        if miss is None and prompt is not None and hit is not None and prompt >= hit:
            miss = prompt - hit
        record = {"phase": self.phase, "model": self.model, "response_model": self.response_model,
                  "streaming": self.streaming, "started_at": self.started_at,
                  "finished_at": datetime.now(UTC).isoformat(), "status": status, "error": error,
                  "latency_ms": round((time.monotonic() - self.started) * 1000),
                  "first_token_ms": self.first_token_ms, "prompt_tokens": prompt,
                  "headers_ms": self.headers_ms, "first_byte_ms": self.first_byte_ms,
                  "first_event_ms": self.first_event_ms, "first_reasoning_ms": self.first_reasoning_ms,
                  "max_data_gap_ms": round(self._max_data_gap_ms) if self._max_data_gap_ms is not None else None,
                  "error_stage": self.stage if status == "failed" else None,
                  "cache_hit_tokens": hit, "cache_miss_tokens": miss, "completion_tokens": completion,
                  "reasoning_tokens": _count((usage.get("completion_tokens_details") or {}).get("reasoning_tokens")),
                  "usage_available": self.usage is not None,
                  "message_hashes": self.message_hashes, "message_characters": self.message_characters,
                  "tools_hash": self.tools_hash}
        logging.getLogger(__name__).info("llm_usage %s", json.dumps(record, ensure_ascii=False))
        if self.sink:
            self.sink(record)


def usage_cost(records: list[dict], *, cache_hit_per_million: str, cache_miss_per_million: str,
               output_per_million: str, currency: str, price_version: str, model: str) -> dict:
    rates = [Decimal(value) for value in (cache_hit_per_million, cache_miss_per_million, output_per_million)]
    if any(not rate.is_finite() or rate < 0 for rate in rates):
        raise ValueError("Token prices must be finite and nonnegative")
    amount = Decimal(0)
    missing = 0
    for record in records:
        if record["model"] != model:
            raise ValueError("Price model must match every request")
        counts = [_count(record.get(key)) for key in ("cache_hit_tokens", "cache_miss_tokens", "completion_tokens")]
        if any(value is None for value in counts):
            missing += 1
            continue
        amount += sum(Decimal(value) * rate for value, rate in zip(counts, rates, strict=True)) / Decimal(1_000_000)
    return {"amount": str(amount) if not missing else None, "known_usage_amount": str(amount),
            "missing_usage_requests": missing, "currency": currency, "price_version": price_version, "model": model,
            "rates_per_million": dict(zip(("cache_hit", "cache_miss", "output"), map(str, rates), strict=True))}
