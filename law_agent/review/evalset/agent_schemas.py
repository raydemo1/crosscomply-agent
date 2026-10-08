"""Schemas for Production Agent evaluation.

This suite evaluates ``agent_runtime.execute_agent_task`` — the stateful
Agent actually delivered to users — not the fixed retrieval pipeline in
``service.py``. Rubrics describe *what must be judged correctly*; they never
prescribe step order (search first, then read, ...), otherwise this would
become a workflow engine test instead of an Agent test.

Deliberately independent from ``schemas.CaseMetricResult`` (retrieval
benchmark): the two suites coexist long term and answer different questions.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from law_agent.data.schemas import StrictModel

AgentSuite = Literal["smoke", "core"]
AgentRunStatus = Literal["succeeded", "failed", "unanswered_gate"]
JudgeGrade = Literal["pass", "minor_issue", "fail", "not_applicable"]
AnswerProvenance = Literal["applicant_statement", "reviewer_instruction"]


class ScriptedAnswer(StrictModel):
    """A pre-authored human answer used to resume a paused Agent during eval."""

    answer: str = Field(min_length=1)
    provenance: AnswerProvenance = "applicant_statement"
    # When None, answers are consumed in pause order. Set it to bind an answer
    # to one specific request_input gate.
    gate_id: str | None = None


class AgentRubric(StrictModel):
    """What the Agent must get right for one case — outcome-level, not steps.

    Deterministic fields (checked without another model):
    ``should_abstain``, ``acceptable_outcomes``, ``must_cover_sources``,
    ``forbidden_clause_sources``, ``expect_freshness_hold``.

    Semantic fields (given to the eval-only judge):
    ``must_not_assume``, ``required_exceptions``, ``clarification_expectations``.
    """

    # Final risk level must be insufficient_evidence iff True; abstaining when
    # the case is answerable (or answering when it is not) is a hard failure.
    should_abstain: bool = False
    # When non-empty, risk_level must be one of these. Empty = outcome level is
    # left to the judge / other deterministic checks.
    acceptable_outcomes: list[str] = Field(default_factory=list)
    # source_ids whose clauses must appear in the final citations.
    must_cover_sources: list[str] = Field(default_factory=list)
    # source_ids that must never appear as can_cite_clause=True citations
    # (guidelines, Q&A, internal policies, ...).
    forbidden_clause_sources: list[str] = Field(default_factory=list)
    expect_freshness_hold: bool = False
    # Judge inputs.
    must_not_assume: list[str] = Field(default_factory=list)
    required_exceptions: list[str] = Field(default_factory=list)
    clarification_expectations: list[str] = Field(default_factory=list)
    notes: str = ""


class AgentCase(StrictModel):
    """One Production Agent evaluation case.

    Material text/question reuse ``EvalScenario`` content (resolved in
    ``agent_cases`` instead of being copied here).
    """

    case_id: str
    question: str
    material_text: str
    rubric: AgentRubric
    # Overrides merged onto the default empty IntakePayload (confirmed facts).
    intake: dict[str, object] = Field(default_factory=dict)
    scripted_answers: list[ScriptedAnswer] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


class IllegalCitation(StrictModel):
    source_id: str
    chunk_id: str
    article_no: str | None = None
    reason: str


class JudgeVerdict(StrictModel):
    """Structured eval-only judge result. Never enters the production path."""

    legal_correctness: JudgeGrade = "not_applicable"
    exception_coverage: JudgeGrade = "not_applicable"
    fact_grounding: JudgeGrade = "not_applicable"
    clarification_quality: JudgeGrade = "not_applicable"
    overall_pass: bool | None = None
    # Short, parseable justifications — one line each, no chain of thought.
    reasons: list[str] = Field(default_factory=list)
    judge_model: str = ""
    # Set when the judge call itself failed; overall_pass stays None so an
    # infra error is not counted as an Agent failure.
    error: str | None = None


class AgentCaseResult(StrictModel):
    case_id: str
    tags: list[str] = Field(default_factory=list)
    status: AgentRunStatus
    risk_level: str | None = None
    # Deterministic checks.
    abstain_correct: bool | None = None
    outcome_acceptable: bool | None = None
    freshness_hold: bool = False
    freshness_hold_correct: bool | None = None
    cited_source_ids: list[str] = Field(default_factory=list)
    missing_required_sources: list[str] = Field(default_factory=list)
    illegal_clause_citations: list[IllegalCitation] = Field(default_factory=list)
    # Agent behaviour counters (diagnostics, not pass/fail by themselves).
    turns: int = 0
    searches: int = 0
    reads: int = 0
    web_searches: int = 0
    enrichment_submissions: int = 0
    request_inputs: int = 0
    duplicate_read_denials: int = 0
    budget_exhausted: bool = False
    questions_asked: list[str] = Field(default_factory=list)
    scripted_answers_used: int = 0
    scripted_answers_unused: int = 0
    # Failure details.
    failure_node: str | None = None
    failure_category: str | None = None
    failure_message: str | None = None
    # Verdicts.
    deterministic_pass: bool = False
    deterministic_fail_reasons: list[str] = Field(default_factory=list)
    judge: JudgeVerdict | None = None
    # True = evaluated and passed; False = evaluated and failed;
    # None = no overall verdict could be formed (judge infra failure).
    overall_pass: bool | None = False
    total_latency_ms: int | None = None


class AgentEvalSummary(StrictModel):
    generated_at: str
    suite: str
    agent_model: str
    judge_model: str | None = None
    rerank_mode: str
    chunks_path: str
    results: list[AgentCaseResult] = Field(default_factory=list)
    total_cases: int = 0
    completed_cases: int = 0
    deterministic_pass_count: int = 0
    judge_pass_count: int = 0
    judge_error_count: int = 0
    overall_pass_count: int = 0
    overall_fail_count: int = 0
    overall_unevaluated_count: int = 0
    mean_turns: float = 0.0
    mean_searches: float = 0.0
    mean_reads: float = 0.0
    total_web_searches: int = 0
    total_request_inputs: int = 0
