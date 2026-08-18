"""Per-review lightweight telemetry counters."""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class WorkflowTelemetry:
    """Counters for one review workflow execution."""

    llm_call_count: int = 0
    retry_count: int = 0


_current: ContextVar[WorkflowTelemetry | None] = ContextVar(
    "lawagent_review_telemetry", default=None
)


def reset_telemetry() -> WorkflowTelemetry:
    """Reset counters for the current execution context."""

    telemetry = WorkflowTelemetry()
    _current.set(telemetry)
    return telemetry


def current_telemetry() -> WorkflowTelemetry:
    """Return counters for the current execution context."""

    telemetry = _current.get()
    if telemetry is None:
        telemetry = reset_telemetry()
    return telemetry


def record_llm_call() -> None:
    """Record one attempted LLM call."""

    current_telemetry().llm_call_count += 1


def record_retry() -> None:
    """Record one retry after a failed call or validation."""

    current_telemetry().retry_count += 1
