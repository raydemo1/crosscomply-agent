"""Schemas for the review evaluation suite (Issue 9)."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from law_agent.review.evalset.manual_labels import MANUAL_LABELS
from law_agent.review.schemas import AgentStep, StrictModel

BadCaseCategory = Literal[
    "retrieval_zero_recall",
    "retrieval_low_recall",
    "candidate_missing",
    "abstention_error",
    "workflow_error",
]
WorkflowOutcome = Literal["clean_success", "degraded_success", "hard_failure"]

# ---------------------------------------------------------------------------
# Golden set case schema
# ---------------------------------------------------------------------------


class EvalScenario(StrictModel):
    """A single golden-set scenario for evaluation."""

    case_id: str
    question: str
    material_text: str
    expected_sources: list[str] = Field(
        default_factory=list,
        description="source_id values that should appear in top results",
    )
    must_have_sources: list[str] = Field(
        default_factory=list,
        description="Expected clause-citable sources required for core legal coverage.",
    )
    optional_supporting_sources: list[str] = Field(
        default_factory=list,
        description="Expected non-citable sources that add context or implementation detail.",
    )
    expected_citation_roles: list[str] = Field(
        default_factory=list,
        description="citation_role values expected in results",
    )
    should_abstain: bool = False
    min_recall_at_5: float = Field(
        default=0.5,
        description="Minimum Recall@5 before a non-abstention case is considered bad.",
    )
    must_not_cite_as_clause: list[str] = Field(
        default_factory=list,
        description="chunk_id values that must NOT appear as can_cite_clause=True",
    )
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def apply_manual_labels(self) -> EvalScenario:
        """Apply manually curated must-have/optional labels.

        All labels are defined in ``manual_labels.py`` — no automatic
        rule-based partitioning is used.
        """
        if self.case_id in MANUAL_LABELS:
            must, opt = MANUAL_LABELS[self.case_id]
            self.must_have_sources = list(must)
            self.optional_supporting_sources = list(opt)
        must_have = set(self.must_have_sources)
        optional = set(self.optional_supporting_sources)
        if must_have & optional:
            raise ValueError("must-have and optional sources must be disjoint")
        if must_have | optional != set(self.expected_sources):
            raise ValueError("must-have and optional sources must partition expected sources")
        return self


# ---------------------------------------------------------------------------
# Per-case eval result
# ---------------------------------------------------------------------------


class CaseMetricResult(StrictModel):
    """Metrics for a single scenario case."""

    case_id: str
    recall_at_3: float | None
    recall_at_5: float | None
    mrr_at_10: float | None
    candidate_recall_at_50: float | None = None
    candidate_unique_source_count: int = 0
    distinct_source_recall_at_5: float | None = None
    must_have_recall_at_5: float | None = None
    optional_coverage_at_5: float | None = None
    duplicate_source_count_at_10: int = 0
    abstention_correct: bool
    # Fact only: did the pipeline trigger second retrieval? No expectation
    # comparison, no bad-case weight — purely diagnostic.
    second_retrieval_triggered: bool = False
    total_latency_ms: int | None = None
    retrieval_latency_ms: int | None = None
    llm_call_count: int = 0
    retry_count: int = 0
    actual_sources: list[str] = Field(default_factory=list)
    missing_sources: list[str] = Field(default_factory=list)
    is_bad_case: bool = False
    bad_reasons: list[str] = Field(default_factory=list)
    bad_case_categories: list[BadCaseCategory] = Field(default_factory=list)
    risk_level: str = ""
    workflow_failed: bool = False
    workflow_outcome: WorkflowOutcome = "clean_success"
    failed_node: str | None = None
    failure_reason: str | None = None
    issue_count: int = 0
    critic_triggered: bool = False
    critic_revised: bool = False
    targeted_retrieval_triggered: bool = False
    critic_reason: str | None = None
    agent_steps: list[AgentStep] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Aggregated metrics
# ---------------------------------------------------------------------------


class ModeMetrics(StrictModel):
    """Aggregated metrics for one retrieval mode."""

    mode: str
    mean_recall_at_3: float
    mean_recall_at_5: float
    mean_mrr_at_10: float
    mean_candidate_recall_at_50: float = 0.0
    mean_distinct_source_recall_at_5: float = 0.0
    mean_must_have_recall_at_5: float = 0.0
    mean_optional_coverage_at_5: float = 0.0
    must_have_case_count: int = 0
    optional_supporting_case_count: int = 0
    mean_duplicate_source_count_at_10: float = 0.0
    abstention_accuracy: float
    # Diagnostic only: fraction of cases where second retrieval fired.
    second_retrieval_trigger_rate: float = 0.0
    mean_total_latency_ms: float | None = None
    mean_retrieval_latency_ms: float | None = None
    total_llm_calls: int = 0
    total_retries: int = 0
    workflow_success_rate: float = 1.0
    clean_success_rate: float = 1.0
    degraded_success_rate: float = 0.0
    hard_failure_rate: float = 0.0
    source_bearing_case_count: int = 0
    critic_trigger_rate: float = 0.0
    critic_revision_rate: float = 0.0
    targeted_retrieval_trigger_rate: float = 0.0
    bad_case_count: int
    bad_case_taxonomy: dict[str, int] = Field(default_factory=dict)
    total_cases: int


class EvalSummary(StrictModel):
    """Full evaluation summary output."""

    generated_at: str
    chunks_path: str
    cases_path: str
    mode_metrics: dict[str, ModeMetrics] = Field(default_factory=dict)
    bad_cases: list[CaseMetricResult] = Field(default_factory=list)
    all_case_results: dict[str, list[CaseMetricResult]] = Field(default_factory=dict)
