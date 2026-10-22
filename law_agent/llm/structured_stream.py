"""Bounded internal SSE reception for structured model responses."""

from __future__ import annotations

import json
import time

import anyio
import httpx
from anyio.from_thread import start_blocking_portal

from law_agent.config import LLMConfig
from law_agent.llm.usage import UsageRequest


class LLMStreamError(RuntimeError):
    pass


class LLMStreamTimeout(TimeoutError):
    pass


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


async def _receive(payload: dict, config: LLMConfig, accounting: UsageRequest) -> str:
    timeout = httpx.Timeout(config.timeout_seconds, read=config.read_timeout_seconds)
    remaining = max(0, config.total_timeout_seconds - (time.monotonic() - accounting.started))
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


def receive_json_response(payload: dict, config: LLMConfig) -> dict:
    payload = {**payload, "stream": True, "stream_options": {"include_usage": True}}
    accounting = UsageRequest(payload)
    accounting.stage = "connect_headers"
    status, error = "failed", None
    try:
        with start_blocking_portal() as portal:
            content = portal.call(_receive, payload, config, accounting)
        accounting.stage = "decode_json"
        result = json.loads(content)
        status = "completed"
        return result
    except Exception as exc:
        error = type(exc).__name__
        raise
    finally:
        accounting.finish(status, error)
