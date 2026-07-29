"""DeepSeek-backed structured LLM nodes for review workflows."""

from __future__ import annotations

import os
from collections.abc import Callable, Sequence
from typing import Generic, TypeVar

from pydantic import BaseModel, ValidationError

from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.telemetry import record_llm_call, record_retry

ModelT = TypeVar("ModelT", bound=BaseModel)


class ReviewWorkflowFailed(RuntimeError):
    """Raised when a review workflow node exhausts retries."""

    def __init__(
        self,
        *,
        failed_node: str,
        reason: str,
        message: str,
        attempts: int,
        trace_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.failed_node = failed_node
        self.reason = reason
        self.message = message
        self.attempts = attempts
        self.trace_id = trace_id

    def to_response(self) -> dict[str, object]:
        return {
            "status": "review_failed",
            "failed_node": self.failed_node,
            "reason": self.reason,
            "message": self.message,
            "attempts": self.attempts,
            "trace_id": self.trace_id,
        }


def max_retries_for_node(node_name: str, default: int = 3) -> int:
    """Return configured retry count for a workflow node."""

    env_by_node = {
        "fact_extraction": "LAWAGENT_LLM_FACT_RETRIES",
        "query_planning": "LAWAGENT_LLM_QUERY_RETRIES",
        "evidence_check": "LAWAGENT_LLM_EVIDENCE_CHECK_RETRIES",
        "result_generation": "LAWAGENT_LLM_RESULT_RETRIES",
        "case_analyst": "LAWAGENT_LLM_ANALYST_RETRIES",
        "evidence_critic": "LAWAGENT_LLM_CRITIC_RETRIES",
        "compliance_reviewer_revision": "LAWAGENT_LLM_RESULT_RETRIES",
    }
    raw = os.getenv(env_by_node.get(node_name, ""), os.getenv("LAWAGENT_LLM_MAX_RETRIES"))
    if raw is None:
        return default
    try:
        retries = int(raw)
    except ValueError:
        return default
    return max(retries, 0)


def model_for_node(node_name: str) -> str | None:
    """Return optional per-node model override."""

    env_by_node = {
        "fact_extraction": "LAWAGENT_LLM_FACT_MODEL",
        "query_planning": "LAWAGENT_LLM_QUERY_MODEL",
        "evidence_check": "LAWAGENT_LLM_EVIDENCE_MODEL",
        "result_generation": "LAWAGENT_LLM_RESULT_MODEL",
        "case_analyst": "LAWAGENT_LLM_ANALYST_MODEL",
        "evidence_critic": "LAWAGENT_LLM_CRITIC_MODEL",
        "compliance_reviewer_revision": "LAWAGENT_LLM_RESULT_MODEL",
    }
    return os.getenv(env_by_node.get(node_name, ""))


class StructuredLLMNode(Generic[ModelT]):
    """Run one DeepSeek JSON node with strict Pydantic validation and retry."""

    def __init__(
        self,
        *,
        node_name: str,
        output_model: type[ModelT],
        client: OpenAICompatibleClient,
        max_retries: int | None = None,
        trace_id: str | None = None,
        structured_output_mode: str | None = None,
    ) -> None:
        self.node_name = node_name
        self.output_model = output_model
        self.client = client
        self.max_retries = (
            max_retries if max_retries is not None else max_retries_for_node(node_name)
        )
        self.model = model_for_node(node_name)
        self.trace_id = trace_id
        # Per-node override for structured output mode. None falls back to
        # client config. result_generation sets "json_object" so the LLM can
        # emit markdown inside string fields without strict_tool schema friction.
        self.structured_output_mode = structured_output_mode

    def run(
        self,
        messages: Sequence[ChatMessage],
        *,
        post_validate: Callable[[ModelT], ModelT] | None = None,
        post_validation_reason: str = "post_validation_failed",
    ) -> ModelT:
        attempts_allowed = self.max_retries + 1
        last_reason = "llm_api_error"
        last_message = f"{self.node_name} failed"

        for attempt in range(1, attempts_allowed + 1):
            try:
                record_llm_call()
                raw = self.client.chat_json(
                    list(messages),
                    output_model=self.output_model,
                    tool_name=self.node_name,
                    model=self.model,
                    structured_output_mode=self.structured_output_mode,
                )
                parsed = self.output_model.model_validate(raw, strict=True)
            except ValidationError as exc:
                last_reason = "pydantic_validation_failed"
                last_message = (
                    f"{self.node_name} output did not match the required JSON schema"
                )
                messages = [
                    *messages,
                    ChatMessage(
                        role="user",
                        content=(
                            "上一轮 JSON 未通过 Pydantic 严格校验。"
                            "请只重新输出符合 schema 的 JSON，不要解释。"
                            f" validation_errors={exc.errors()}"
                        ),
                    ),
                ]
                if attempt == attempts_allowed:
                    raise ReviewWorkflowFailed(
                        failed_node=self.node_name,
                        reason=last_reason,
                        message=last_message,
                        attempts=attempt,
                        trace_id=self.trace_id,
                    ) from exc
                record_retry()
            except Exception as exc:
                last_reason = "llm_api_error"
                last_message = f"{self.node_name} LLM call failed: {exc}"
                messages = [
                    *messages,
                    ChatMessage(
                        role="user",
                        content=(
                            "上一轮 LLM 调用或 JSON 解析失败。"
                            "请只重新输出符合 schema 的 JSON/tool arguments，不要解释。"
                            f" error={exc}"
                        ),
                    ),
                ]
                if attempt == attempts_allowed:
                    raise ReviewWorkflowFailed(
                        failed_node=self.node_name,
                        reason=last_reason,
                        message=last_message,
                        attempts=attempt,
                        trace_id=self.trace_id,
                    ) from exc
                record_retry()
            else:
                if post_validate is None:
                    return parsed
                try:
                    return post_validate(parsed)
                except ValueError as exc:
                    last_reason = post_validation_reason
                    last_message = str(exc)
                    messages = [
                        *messages,
                        ChatMessage(
                            role="user",
                            content=(
                                "上一轮 JSON 通过了 schema，但 claim grounding 业务校验失败。"
                                "请只重新输出符合 schema 的 JSON，并确保每个 claim 至少引用一个"
                                "允许且可作为法条依据的 supporting_chunk_id。"
                                f" validation_error={exc}"
                            ),
                        ),
                    ]
                    if attempt == attempts_allowed:
                        raise ReviewWorkflowFailed(
                            failed_node=self.node_name,
                            reason=last_reason,
                            message=last_message,
                            attempts=attempt,
                            trace_id=self.trace_id,
                        ) from exc
                    record_retry()

        raise ReviewWorkflowFailed(
            failed_node=self.node_name,
            reason=last_reason,
            message=last_message,
            attempts=attempts_allowed,
            trace_id=self.trace_id,
        )
