"""Bounded internal SSE reception for structured model responses."""

from __future__ import annotations

import json
import time
from collections.abc import Callable

import anyio
import httpx
from anyio.from_thread import start_blocking_portal

from law_agent.config import LLMConfig
from law_agent.llm.usage import UsageRequest


class LLMStreamError(RuntimeError):
    pass


class LLMStreamTimeout(TimeoutError):
    pass


class _AnswerFieldStream:
    def __init__(self) -> None:
        self.buffer = ''
        self.phase = 'object'
        self.decoder = json.JSONDecoder()
        self.key = None

    def feed(self, piece: str) -> str:
        if self.phase == 'done':
            return ''
        self.buffer += piece
        out = []
        while self.buffer:
            if self.phase == 'string':
                char = self.buffer[0]
                if char == '"':
                    self.buffer = ''
                    self.phase = 'done'
                    break
                if char != chr(92):
                    out.append(char)
                    self.buffer = self.buffer[1:]
                    continue
                if len(self.buffer) < 2:
                    break
                size = 6 if self.buffer[1] == 'u' else 2
                if len(self.buffer) < size:
                    break
                if size == 6:
                    try:
                        code = int(self.buffer[2:6], 16)
                    except ValueError as exc:
                        raise LLMStreamError('Invalid answer Unicode escape') from exc
                    if 0xD800 <= code <= 0xDBFF:
                        if len(self.buffer) < 12:
                            break
                        size = 12
                try:
                    out.append(json.loads('"' + self.buffer[:size] + '"'))
                except ValueError as exc:
                    raise LLMStreamError('Invalid answer escape') from exc
                self.buffer = self.buffer[size:]
                continue
            self.buffer = self.buffer.lstrip()
            if not self.buffer:
                break
            if self.phase == 'object':
                if self.buffer[0] != '{':
                    raise LLMStreamError('Expected a JSON object')
                self.buffer = self.buffer[1:]
                self.phase = 'key'
            elif self.phase == 'key':
                if self.buffer[0] == '}':
                    self.phase = 'done'
                    self.buffer = ''
                    break
                if self.buffer[0] == ',':
                    self.buffer = self.buffer[1:]
                    continue
                try:
                    self.key, end = self.decoder.raw_decode(self.buffer)
                except ValueError:
                    break
                self.buffer = self.buffer[end:]
                self.phase = 'colon'
            elif self.phase == 'colon':
                if self.buffer[0] != ':':
                    raise LLMStreamError('Expected a JSON field separator')
                self.buffer = self.buffer[1:]
                self.phase = 'value'
            elif self.phase == 'value':
                if self.key == 'answer':
                    if self.buffer[0] != '"':
                        raise LLMStreamError('Expected an answer string')
                    self.buffer = self.buffer[1:]
                    self.phase = 'string'
                else:
                    try:
                        _, end = self.decoder.raw_decode(self.buffer)
                    except ValueError:
                        break
                    if end == len(self.buffer):
                        break
                    self.buffer = self.buffer[end:]
                    self.phase = 'key'
        return ''.join(out)


class _ObservedStream(httpx.AsyncByteStream):
    def __init__(self, stream, accounting):
        self.stream, self.accounting = stream, accounting

    async def __aiter__(self):
        async for chunk in self.stream:
            if chunk:
                self.accounting.data()
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


async def _receive(payload: dict, config: LLMConfig, accounting: UsageRequest, on_answer_delta: Callable[[str], None] | None = None) -> str:
    timeout = httpx.Timeout(config.timeout_seconds, read=config.read_timeout_seconds)
    remaining = max(0, config.total_timeout_seconds - (time.monotonic() - accounting.started))
    answer_stream = _AnswerFieldStream() if on_answer_delta is not None else None
    try:
        with anyio.fail_after(remaining):
            client = httpx.AsyncClient(timeout=timeout)
            try:
                request = client.build_request(
                    "POST", f"{config.base_url}/chat/completions", json=payload,
                    headers={"Authorization": f"Bearer {config.api_key}", "Accept": "text/event-stream"},
                )
                response = await client.send(request, stream=True)
                try:
                    accounting.headers()
                    response.raise_for_status()
                    if "text/event-stream" not in response.headers.get("content-type", ""):
                        raise LLMStreamError("LLM JSON stream did not return text/event-stream")
                    response.stream = _ObservedStream(response.stream, accounting)
                    content, event_data = [], []
                    finish_reason = None
                    async for line in response.aiter_lines():
                        if line.startswith("data:"):
                            event_data.append(line[5:].lstrip(" "))
                            continue
                        if line or not event_data:
                            continue
                        data = "\n".join(event_data)
                        event_data = []
                        if data == "[DONE]":
                            if finish_reason != "stop":
                                raise LLMStreamError("LLM JSON stream ended without a complete stop")
                            return "".join(content)
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError as exc:
                            raise LLMStreamError("LLM JSON stream returned a malformed event") from exc
                        accounting.event()
                        accounting.observe(event)
                        if event.get("error"):
                            raise LLMStreamError("LLM JSON stream returned an error")
                        for choice in event.get("choices", []):
                            if choice.get("index", 0) != 0:
                                raise LLMStreamError("LLM JSON stream returned multiple choices")
                            delta = choice.get("delta", {})
                            if delta.get("tool_calls"):
                                raise LLMStreamError("LLM JSON stream returned an unexpected tool call")
                            if delta.get("reasoning_content"):
                                accounting.reasoning()
                            if delta.get("content"):
                                if finish_reason is not None:
                                    raise LLMStreamError("LLM JSON stream returned content after completion")
                                accounting.token()
                                content.append(delta["content"])
                                if answer_stream is not None:
                                    piece = answer_stream.feed(delta["content"])
                                    if piece:
                                        on_answer_delta(piece)
                            if choice.get("finish_reason"):
                                finish_reason = choice["finish_reason"]
                                if finish_reason != "stop":
                                    raise LLMStreamError(f"LLM JSON stream ended with {finish_reason}")
                    raise LLMStreamError("LLM JSON stream interrupted before [DONE]")
                finally:
                    with anyio.CancelScope(shield=True):
                        await response.aclose()
            finally:
                with anyio.CancelScope(shield=True):
                    await client.aclose()
    except httpx.TimeoutException as exc:
        accounting.stage = "connect_headers" if isinstance(exc, httpx.ConnectTimeout) else accounting.stage
        raise LLMStreamTimeout(f"LLM {accounting.stage} timeout ({type(exc).__name__})") from exc
    except TimeoutError as exc:
        accounting.stage = "total_deadline"
        raise LLMStreamTimeout(f"LLM request exceeded total timeout ({config.total_timeout_seconds}s)") from exc
    except httpx.HTTPStatusError as exc:
        raise LLMStreamError(f"LLM JSON streaming request failed with HTTP {exc.response.status_code}") from exc


def receive_json_response(
    payload: dict, config: LLMConfig,
    *, on_answer_delta: Callable[[str], None] | None = None,
) -> dict:
    payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
    accounting = UsageRequest(payload)
    accounting.stage = "connect_headers"
    status, error = "failed", None
    try:
        with start_blocking_portal() as portal:
            content = portal.call(_receive, payload, config, accounting, on_answer_delta)
        accounting.stage = "decode_json"
        result = json.loads(content)
        status = "completed"
        return result
    except Exception as exc:
        error = type(exc).__name__
        raise
    finally:
        accounting.finish(status, error)
