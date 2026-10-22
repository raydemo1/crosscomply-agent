"""Bounded, evaluation-only QA requests; never changes the production client."""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import anyio
import httpx
from anyio.from_thread import start_blocking_portal

from law_agent.llm.openai_compatible import ChatMessage
from law_agent.llm.usage import UsageRequest
from law_agent.review.qa_citations import CitationReferenceError, render_qa_citations


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_new(path, value):
    with Path(path).open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


@dataclass(frozen=True)
class Profile:
    name: str
    model: str
    base_url: str
    key_env: str
    input_cny: str
    output_cny: str
    provider: str
    beta_base_url: str = ""

    def __post_init__(self):
        if self.provider not in {"deepseek", "siliconflow"}:
            raise ValueError("Unknown diagnostic provider")
        for rate in (self.input_cny, self.output_cny):
            if not Decimal(rate).is_finite() or Decimal(rate) <= 0:
                raise ValueError("Positive, finite conservative prices required")


class BudgetStopped(RuntimeError):
    pass


class Ledger:
    """Reserve before sending; unanswered requests keep their full reservation."""

    def __init__(self, path: Path, limit="30"):
        self.path, self.limit = path, Decimal(limit)
        if not self.limit.is_finite() or self.limit <= 0:
            raise ValueError("Positive finite budget required")
        self.entries = [json.loads(s) for s in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    @property
    def spent(self):
        requests = {}
        for row in self.entries:
            requests[row["id"]] = Decimal(row["charged_upper_cny"])
        return sum(requests.values(), Decimal(0))

    def append(self, row):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
        self.entries.append(row)

    def reserve(self, request_id, payload, profile):
        if any(row["id"] == request_id for row in self.entries):
            raise BudgetStopped("Request already attempted; resampling is disabled")
        # UTF-8 bytes bound byte-level tokenization; include serialization overhead.
        input_bound = len(json.dumps(payload, ensure_ascii=False).encode()) + 4096
        output_bound = payload["max_tokens"] + payload.get("thinking_budget", 0)
        amount = (Decimal(input_bound) * Decimal(profile.input_cny)
                  + Decimal(output_bound) * Decimal(profile.output_cny)) / 1_000_000
        if self.spent + amount > self.limit:
            raise BudgetStopped("Next request exceeds the remaining conservative budget")
        self.append({"id": request_id, "state": "reserved", "model": profile.model,
                     "charged_upper_cny": str(amount), "input_token_bound": input_bound,
                     "output_token_bound": output_bound})
        return amount

    def settle(self, request_id, usage, profile, reserved):
        prompt, completion = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if type(prompt) is not int or type(completion) is not int or min(prompt, completion) < 0:
            return
        upper = (Decimal(prompt) * Decimal(profile.input_cny)
                 + Decimal(completion) * Decimal(profile.output_cny)) / 1_000_000
        self.append({"id": request_id, "state": "settled", "model": profile.model,
                     "charged_upper_cny": str(upper), "usage": usage})
        if upper > reserved or self.spent > self.limit:
            raise BudgetStopped("Provider usage exceeded reservation; stop and audit")


class DiagnosticClient:
    """Explicit provider payloads with one HTTP attempt and persistent accounting."""

    def __init__(self, profile, ledger, output, prefix, *, max_tokens=8192,
                 timeout=30, total_timeout=300):
        self.profile, self.ledger, self.output, self.prefix = profile, ledger, output, prefix
        self.max_tokens, self.timeout, self.calls = max_tokens, timeout, 0
        self.total_timeout = total_timeout

    def payload(self, messages, tools, *, tool_choice="auto"):
        body = {"model": self.profile.model, "messages": [m.to_dict() for m in messages],
                "stream": True, "stream_options": {"include_usage": True},
                "max_tokens": self.max_tokens}
        if tools:
            body.update(tools=tools, tool_choice=tool_choice)
        if self.profile.provider == "deepseek":
            body.update(thinking={"type": "enabled"}, reasoning_effort="low")
        else:
            body.update(enable_thinking=True, thinking_budget=4096)
        return body

    async def _receive_stream(self, payload, base, key, accounting, transport):
        content, reasoning, calls, event_data = [], [], {}, []
        finish = None
        remaining = max(0, self.total_timeout - (time.monotonic() - accounting.started))
        try:
            with anyio.fail_after(remaining):
                async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout)) as client:
                    async with client.stream("POST", base.rstrip("/") + "/chat/completions",
                            json=payload, headers={"Authorization": "Bearer " + key,
                                "Accept": "text/event-stream", "X-Trace-Id": transport["custom_trace_id"]}) as response:
                        accounting.headers()
                        transport.update(http_status=response.status_code,
                            server_trace_id=response.headers.get("x-siliconcloud-trace-id"))
                        if response.is_error:
                            body = await response.aread()
                            try:
                                parsed = json.loads(body)
                                transport["provider_error"] = {
                                    k: str(parsed[k]).replace(key, "[redacted]")[:1000]
                                    for k in ("code", "message", "error") if k in parsed}
                            except (ValueError, TypeError):
                                pass
                            response.raise_for_status()
                        if "text/event-stream" not in response.headers.get("content-type", ""):
                            raise RuntimeError("Expected SSE response")
                        async for line in response.aiter_lines():
                            accounting.data()
                            if line.startswith("data:"):
                                event_data.append(line[5:].lstrip())
                                continue
                            if line or not event_data:
                                continue
                            data, event_data = "\n".join(event_data), []
                            if data == "[DONE]":
                                if finish not in {"stop", "tool_calls"}:
                                    raise RuntimeError("SSE ended without a complete finish")
                                if finish == "tool_calls" and not calls:
                                    raise RuntimeError("SSE ended without declared tool calls")
                                message = {"role": "assistant", "content": "".join(content) or None}
                                if reasoning:
                                    message["reasoning_content"] = "".join(reasoning)
                                if calls:
                                    message["tool_calls"] = [calls[i] for i in sorted(calls)]
                                return {"model": accounting.response_model or self.profile.model,
                                    "choices": [{"message": message, "finish_reason": finish}],
                                    "usage": accounting.usage}
                            event = json.loads(data)
                            accounting.event()
                            accounting.observe(event)
                            if event.get("error"):
                                raise RuntimeError("Provider returned an SSE error")
                            for choice in event.get("choices", []):
                                if choice.get("index", 0) != 0:
                                    raise RuntimeError("Unexpected additional completion")
                                delta = choice.get("delta", {})
                                if finish and any(delta.get(k) for k in ("content", "reasoning_content", "tool_calls")):
                                    raise RuntimeError("SSE content after completion")
                                if delta.get("reasoning_content"):
                                    accounting.reasoning()
                                    reasoning.append(delta["reasoning_content"])
                                if delta.get("content"):
                                    accounting.token()
                                    content.append(delta["content"])
                                for part in delta.get("tool_calls") or []:
                                    call = calls.setdefault(part["index"], {"id": "", "type": "function",
                                        "function": {"name": "", "arguments": ""}})
                                    if part.get("id"):
                                        call["id"] += part["id"]
                                    for field in ("name", "arguments"):
                                        call["function"][field] += (part.get("function") or {}).get(field, "")
                                if choice.get("finish_reason"):
                                    finish = choice["finish_reason"]
                                    if finish not in {"stop", "tool_calls"}:
                                        raise RuntimeError("completion_" + finish)
                        raise RuntimeError("SSE interrupted before DONE")
        except httpx.TimeoutException as exc:
            accounting.stage = "connect_headers" if isinstance(exc, httpx.ConnectTimeout) else accounting.stage
            raise
        except TimeoutError:
            accounting.stage = "total_deadline"
            raise

    def request(self, payload):
        key = os.environ.get(self.profile.key_env)
        if not key:
            raise BudgetStopped(f"Missing process credential: {self.profile.key_env}")
        request_id = f"{self.prefix}.{self.calls}"
        self.calls += 1
        reserved = self.ledger.reserve(request_id, payload, self.profile)
        path = self.output / request_id
        write_new(path.with_suffix(path.suffix + ".request.json"), payload)
        strict = any(t.get("function", {}).get("strict") for t in payload.get("tools", []))
        base = self.profile.beta_base_url if strict and self.profile.provider == "deepseek" else self.profile.base_url
        if not base:
            raise BudgetStopped("Explicit provider endpoint required")
        accounting = UsageRequest(payload)
        accounting.stage = "connect_headers"
        started = time.monotonic()
        response, error, status = None, None, "failed"
        transport = {"custom_trace_id": str(uuid.uuid4())}
        try:
            with start_blocking_portal() as portal:
                response = portal.call(self._receive_stream, payload, base, key, accounting, transport)
            self.ledger.settle(request_id, response.get("usage") or {}, self.profile, reserved)
            status = "completed"
            return response
        except httpx.HTTPStatusError as exc:
            error = f"HTTP {exc.response.status_code}"
            raise RuntimeError(error) from None
        except Exception as exc:
            error = type(exc).__name__
            raise
        finally:
            accounting.finish(status, error)
            write_new(path.with_suffix(path.suffix + ".response.json"), {
                "response": response, "error": error, **transport,
                "timing": {"headers_ms": accounting.headers_ms,
                    "first_event_ms": accounting.first_event_ms,
                    "first_reasoning_ms": accounting.first_reasoning_ms,
                    "first_text_ms": accounting.first_token_ms,
                    "error_stage": accounting.stage if error else None},
                "latency_ms": round((time.monotonic()-started)*1000)})

    def chat_tools(self, messages, *, tools, model=None, on_text=None):
        if model is not None and model != self.profile.model:
            raise ValueError("Model override would violate the frozen profile")
        response = self.request(self.payload(messages, tools))
        choice = response["choices"][0]
        if choice.get("finish_reason") not in {"stop", "tool_calls"}:
            raise RuntimeError("completion_" + str(choice.get("finish_reason")))
        message = choice["message"]
        if on_text and message.get("content"):
            on_text(message["content"])
        return message

    def stream_text(self, messages, *, model=None, tools=None):
        response = self.request(self.payload(messages, tools, tool_choice="none"))
        choice = response["choices"][0]
        if choice.get("finish_reason") != "stop" or choice["message"].get("tool_calls"):
            raise RuntimeError("Answer did not finish with plain text")
        yield choice["message"].get("content") or ""


def answer_once(client, packet):
    started = time.monotonic()
    raw, answer, citations, error = None, None, [], None
    try:
        response = client.request(client.payload([ChatMessage(**m) for m in packet["messages"]],
                                  packet.get("tools", []), tool_choice="none"))
        choice = response["choices"][0]
        raw = choice["message"]
        if choice.get("finish_reason") != "stop" or raw.get("tool_calls"):
            return {"raw_response": raw, "answer": None, "citations": [],
                    "error": "completion_" + str(choice.get("finish_reason")),
                    "latency_ms": round((time.monotonic()-started)*1000),
                    "input_sha256": digest(packet["messages"])}
        answer, citations = render_qa_citations(raw.get("content") or "", packet["citations"])
        if not answer.strip():
            error = "empty_answer"
    except CitationReferenceError:
        error = "citation_failure"
    except BudgetStopped:
        raise
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, httpx.HTTPError) as exc:
        error = str(exc) if str(exc).startswith(("HTTP ", "completion_")) else type(exc).__name__
    return {"raw_response": raw, "answer": answer, "citations": citations, "error": error,
            "latency_ms": round((time.monotonic()-started)*1000), "input_sha256": digest(packet["messages"])}


def selection_gate(rows, reviews, case_ids):
    expected = {(cid, arm) for cid in case_ids for arm in ("baseline", "candidate")}
    keys = [(r["case_id"], r["arm"]) for r in rows]
    if len(keys) != len(expected) or set(keys) != expected or len(reviews) != len(expected):
        return {"eligible": False, "reason": "incomplete_results_or_reviews"}
    verdicts = {(r["case_id"], r["arm"]): r for r in reviews}
    if set(verdicts) != expected:
        return {"eligible": False, "reason": "invalid_review_coverage"}
    for row in rows:
        review = verdicts[(row["case_id"], row["arm"])]
        if row["error"] or review.get("result_sha256") != digest(row):
            return {"eligible": False, "reason": "delivery_failure_or_unbound_review"}
        if (type(review.get("target_error")) is not bool
                or type(review.get("material_error")) is not bool
                or not review.get("reason") or not review.get("evidence")):
            return {"eligible": False, "reason": "unreviewed"}
        if (review["target_error"] or review["material_error"]) and not review.get("quote"):
            return {"eligible": False, "reason": "missing_error_quote"}
        raw_text = (row.get("raw_response") or {}).get("content") or row.get("answer") or ""
        if review.get("quote") and review["quote"] not in raw_text:
            return {"eligible": False, "reason": "error_quote_not_in_answer"}
    improvement = sum(verdicts[cid, "baseline"]["target_error"]
                      - verdicts[cid, "candidate"]["target_error"] for cid in case_ids)
    for cid in case_ids:
        if type(verdicts[cid, "candidate"].get("new_material_error")) is not bool:
            return {"eligible": False, "reason": "unreviewed_pairwise_material_errors"}
    regressions = [cid for cid in case_ids if verdicts[cid, "candidate"]["new_material_error"]]
    return {"eligible": improvement >= 2 and not regressions,
            "target_error_reduction": improvement, "new_material_errors": regressions,
            "reason": "single_sample_screen_only"}
