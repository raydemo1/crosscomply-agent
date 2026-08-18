"""Schemas for material-driven compliance review runs."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator

from law_agent.data.schemas import Authority, ClauseCitationRole, DocType, LawStatus, StrictModel

ReviewInputMode = Literal["pasted_text", "uploaded_file"]
ReviewMode = Literal["llm", "multi_agent"]
RiskLevel = Literal["high", "medium", "low", "insufficient_evidence"]
_SEMANTIC_NULL_STRINGS = frozenset({"null", "none", "unknown", "n/a", "未知", "未提供", "未说明"})
RetrievalQueryType = Literal[
    "legal_issue",
    "material_fact",
    "region_condition",
    "industry_condition",
    "missing_information",
]
RetrieverName = Literal["keyword", "hybrid", "elasticsearch", "pgvector"]
EvidenceStatus = Literal["not_checked", "sufficient", "needs_second_retrieval", "insufficient"]
CitationUsage = Literal[
    "legal_basis",
    "conditional_basis",
    "implementation_reference",
    "policy_explanation",
]
EvidenceIssueType = Literal[
    "no_primary_legal_basis",
    "region_mismatch",
    "industry_mismatch",
    "only_auxiliary_evidence",
    "cross_border_mismatch",
    "critical_facts_missing",
]


class ReviewFacts(StrictModel):
    """Structured facts extracted from user material."""

    business_activity: str | None = None
    data_types: list[str] = Field(default_factory=list)
    sensitive_personal_info: bool | None = None
    cross_border_transfer: bool | None = None
    overseas_recipient: str | None = None
    processing_purpose: str | None = None
    legal_basis_or_consent: str | None = None
    industry: str | None = None
    region: str | None = None
    missing_information: list[str] = Field(default_factory=list)

    @field_validator(
        "business_activity",
        "overseas_recipient",
        "processing_purpose",
        "legal_basis_or_consent",
        "industry",
        "region",
        mode="before",
    )
    @classmethod
    def normalize_semantic_null_strings(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().casefold() in _SEMANTIC_NULL_STRINGS:
            return None
        return value


class UploadedFileMeta(StrictModel):
    """Metadata for a local file used as review material."""

    filename: str
    local_path: str
    raw_format: str


class MaterialRecord(StrictModel):
    """Normalized review material input."""

    input_mode: ReviewInputMode = "pasted_text"
    material_text: str
    source_name: str | None = None
    parser: str = "pasted_text"
    parser_version: str = "0.1.0"
    uploaded_file: UploadedFileMeta | None = None

    @field_validator("material_text")
    @classmethod
    def material_text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("material_text must not be blank")
        return value


class RetrievalQuery(StrictModel):
    """A typed query generated for retrieval."""

    query_id: str
    query_type: RetrievalQueryType
    text: str
    pathway: str | None = None


class RetrievalHit(StrictModel):
    """A scored evidence hit returned by a retriever."""

    chunk_id: str
    doc_id: str
    source_id: str
    title: str
    text: str
    score: float
    rank: int
    retriever: RetrieverName
    citation_role: ClauseCitationRole
    can_cite_clause: bool
    source_url: str
    matched_query_type: RetrievalQueryType | None = None
    # Chunk-level structured fields surfaced for citation cards and inline
    # clause references. ``article_no`` and ``citation_label`` allow the
    # frontend to render a proper legal citation (e.g. "《个人信息保护法》
    # 第三十九条") instead of just the chunk title. ``heading_path`` carries
    # the chapter/section context for the evidence panel.
    article_no: str | None = None
    citation_label: str | None = None
    heading_path: list[str] = Field(default_factory=list)
    doc_type: DocType = "law"
    authority: Authority = "unknown"
    law_status: LawStatus = "unknown"
    publish_date: str | None = None
    effective_date: str | None = None
    issuing_body: str | None = None
    # Only populated for a citable legal article. ``text`` remains the
    # retrieval passage; this field is the complete article assembled from
    # all chunks belonging to the same source/article.
    full_article_text: str | None = None


class SourceEvidencePacket(StrictModel):
    """Evidence retained for one selected source.

    ``representative_chunk`` is the source-level hit used for final source
    diversity. ``supporting_chunks`` and ``neighbor_chunks`` preserve
    chunk-level context so result generation and citation expansion do not
    lose more precise passages from the same source.
    """

    source_id: str
    title: str
    representative_chunk: RetrievalHit
    supporting_chunks: list[RetrievalHit] = Field(default_factory=list)
    neighbor_chunks: list[RetrievalHit] = Field(default_factory=list)


class EvidenceIssue(StrictModel):
    """A specific evidence sufficiency issue detected during self-check."""

    issue_type: EvidenceIssueType
    description: str


class SecondRetrievalPlan(StrictModel):
    """Plan for one controlled second retrieval when evidence is weak."""

    expanded_queries: list[RetrievalQuery] = Field(default_factory=list)
    increased_top_k: int = 20
    stronger_boost: bool = True
    reason: str


class EvidenceSelfCheck(StrictModel):
    """Evidence sufficiency state for a review trace."""

    status: EvidenceStatus
    issues: list[EvidenceIssue] = Field(default_factory=list)
    triggered_reasons: list[str] = Field(default_factory=list)
    second_retrieval_triggered: bool = False
    second_retrieval_plan: SecondRetrievalPlan | None = None


class Citation(StrictModel):
    """A governed citation selected for a review result."""

    source_id: str
    chunk_id: str
    title: str
    source_url: str
    citation_role: ClauseCitationRole
    can_cite_clause: bool
    usage: CitationUsage
    citation_label: str | None = None
    citation_ref: str = ""
    article_no: str | None = None
    full_article_text: str | None = None
    doc_type: DocType = "law"
    authority: Authority = "unknown"
    law_status: LawStatus = "unknown"
    publish_date: str | None = None
    effective_date: str | None = None
    issuing_body: str | None = None
    heading_path: list[str] = Field(default_factory=list)


class CitationGroup(StrictModel):
    """A group of citations sharing the same usage category."""

    usage: CitationUsage
    citations: list[Citation] = Field(default_factory=list)
    scope_note: str | None = None


class GroundedClaim(StrictModel):
    """One conclusion claim and the evidence chunks that support it."""

    text: str
    supporting_chunk_ids: list[str] = Field(default_factory=list)
    supporting_citation_refs: list[str] = Field(default_factory=list)


class ReviewIssue(StrictModel):
    """One bounded legal issue identified by the case analyst."""

    issue_id: str
    question: str
    query_ids: list[str] = Field(default_factory=list)
    query_types: list[RetrievalQueryType] = Field(default_factory=list)
    research_queries: list[str] = Field(default_factory=list, max_length=3)
    required_evidence_roles: list[ClauseCitationRole] = Field(default_factory=list)
    priority: Literal["high", "medium", "low"] = "medium"


class IssuePlan(StrictModel):
    """Deterministic case-analyst output consumed by research and critique."""

    issues: list[ReviewIssue] = Field(default_factory=list, max_length=5)


class IssueDraft(StrictModel):
    """One issue-planning node output before queries have been generated."""

    question: str = Field(min_length=1)
    query_types: list[RetrievalQueryType] = Field(min_length=1)
    required_evidence_roles: list[ClauseCitationRole] = Field(default_factory=list)
    priority: Literal["high", "medium", "low"] = "medium"


class IssuePlanDraft(StrictModel):
    """Strict output of the Case Analyst issue-planning step."""

    issues: list[IssueDraft] = Field(min_length=1, max_length=4)


class EvidenceDossier(StrictModel):
    """Deterministic evidence handoff for one review issue."""

    issue_id: str
    evidence_chunk_ids: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    evidence_gap: bool = False
    coverage_status: Literal["covered", "partial", "missing"] = "missing"
    missing_evidence_roles: list[ClauseCitationRole] = Field(default_factory=list)


class IssueResearchResult(StrictModel):
    """Tool-backed research output produced by one Evidence Researcher."""

    issue_id: str
    executed_queries: list[RetrievalQuery] = Field(default_factory=list)
    keyword_hits: list[RetrievalHit] = Field(default_factory=list)
    vector_hits: list[RetrievalHit] = Field(default_factory=list)
    candidate_hits: list[RetrievalHit] = Field(default_factory=list)
    evidence_hits: list[RetrievalHit] = Field(default_factory=list)


class CaseAnalysis(StrictModel):
    """Case-analyst output with issue-specific queries ready for retrieval."""

    facts: ReviewFacts
    issue_plan: IssuePlan
    queries: list[RetrievalQuery] = Field(default_factory=list)


class TargetedRetrievalRequest(StrictModel):
    """One bounded evidence gap that the critic asks researchers to refill."""

    issue_id: str
    query: str
    query_type: RetrievalQueryType = "legal_issue"
    reason: str

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("targeted retrieval query must not be blank")
        return value.strip()


RevisionOperation = Literal[
    "remove_claim",
    "narrow_claim",
    "add_supported_claim",
    "mark_evidence_gap",
    "change_risk_boundary",
    "abstain",
]


class RevisionAction(StrictModel):
    """One evidence-constrained operation requested by the Critic."""

    operation: RevisionOperation
    reason: str
    issue_id: str | None = None
    claim_index: int | None = Field(default=None, ge=0)
    replacement_text: str | None = None
    supporting_chunk_ids: list[str] = Field(default_factory=list)


class ClaimReplacement(StrictModel):
    """Replace one existing grounded claim without regenerating the result."""

    claim_index: int = Field(ge=0)
    claim: GroundedClaim


class ReviewResultPatch(StrictModel):
    """Bounded delta applied to an already validated ReviewResult."""

    risk_level: RiskLevel | None = None
    conclusion: str | None = None
    remove_claim_indexes: list[int] = Field(default_factory=list)
    replace_claims: list[ClaimReplacement] = Field(default_factory=list)
    add_claims: list[GroundedClaim] = Field(default_factory=list)
    append_missing_information: list[str] = Field(default_factory=list)
    append_recommended_actions: list[str] = Field(default_factory=list)
    append_risk_boundaries: list[str] = Field(default_factory=list)


class CritiqueDecision(StrictModel):
    """Evidence critic decision; at most one revision is allowed."""

    decision: Literal["accept", "research_required", "revision_required"]
    unsupported_claims: list[str] = Field(default_factory=list)
    missing_issue_ids: list[str] = Field(default_factory=list)
    revision_instructions: list[str] = Field(default_factory=list)
    revision_actions: list[RevisionAction] = Field(default_factory=list, max_length=5)
    targeted_retrieval_requests: list[TargetedRetrievalRequest] = Field(
        default_factory=list, max_length=3
    )
    reason: str

    @model_validator(mode="after")
    def decision_requires_consistent_route(self) -> CritiqueDecision:
        needs_revision = self.decision in {
            "research_required",
            "revision_required",
        }
        if needs_revision and not self.revision_instructions and not self.revision_actions:
            raise ValueError(f"{self.decision} decision requires revision actions")
        if self.decision == "accept" and self.targeted_retrieval_requests:
            raise ValueError("accept decision cannot request targeted retrieval")
        if self.decision == "accept" and self.revision_actions:
            raise ValueError("accept decision cannot request revision actions")
        if self.decision == "accept" and self.revision_instructions:
            raise ValueError("accept decision cannot request revision instructions")
        if self.decision == "research_required" and not self.targeted_retrieval_requests:
            raise ValueError("research_required decision requires targeted retrieval")
        if self.decision == "revision_required" and self.targeted_retrieval_requests:
            raise ValueError("revision_required decision cannot request targeted retrieval")
        return self


class AgentStep(StrictModel):
    """Compact trace record for one business-agent execution."""

    agent_name: Literal[
        "case_analyst",
        "evidence_researcher",
        "compliance_reviewer",
        "evidence_critic",
    ]
    status: Literal["completed", "skipped", "failed"]
    decision: str | None = None
    latency_ms: int = 0
    llm_calls: int = 0


class ReviewResult(StrictModel):
    """Structured review result produced from facts and evidence."""

    review_result_id: str
    review_case_id: str
    trace_id: str
    risk_level: RiskLevel
    conclusion: str
    review_facts: ReviewFacts
    trigger_reasons: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    risk_boundaries: list[str] = Field(default_factory=list)
    claims: list[GroundedClaim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    applicable_evidence: list[CitationGroup] = Field(default_factory=list)


class ReviewCase(StrictModel):
    """A material-driven compliance review case."""

    review_case_id: str
    created_at: str
    question: str
    material: MaterialRecord
    review_facts: ReviewFacts
    review_mode: ReviewMode = "llm"
    trace_id: str
    latest_result_id: str | None = None
    user_feedback: dict[str, str] = Field(default_factory=dict)

    @field_validator("question")
    @classmethod
    def question_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("question must not be blank")
        return value


class RetrievalTrace(StrictModel):
    """Verbose retrieval and evidence trace for review debugging and eval."""

    trace_id: str
    review_case_id: str
    created_at: str
    evidence_self_check: EvidenceSelfCheck
    queries: list[RetrievalQuery] = Field(default_factory=list)
    filters: dict[str, object] = Field(default_factory=dict)
    metadata_boosts: dict[str, float] = Field(default_factory=dict)
    keyword_results: list[RetrievalHit] = Field(default_factory=list)
    vector_results: list[RetrievalHit] = Field(default_factory=list)
    hybrid_results: list[RetrievalHit] = Field(default_factory=list)
    candidate_results: list[RetrievalHit] = Field(
        default_factory=list,
        description="Unique fused chunk candidates before final source allocation.",
    )
    neighbor_chunks: list[RetrievalHit] = Field(default_factory=list)
    rerank: dict[str, object] = Field(default_factory=dict)
    second_retrieval: dict[str, object] = Field(default_factory=dict)
    final_evidence: list[RetrievalHit] = Field(default_factory=list)
    source_evidence_packets: list[SourceEvidencePacket] = Field(default_factory=list)
    citation_validation: dict[str, object] = Field(default_factory=dict)
    issue_plan: IssuePlan | None = None
    issue_research_results: list[IssueResearchResult] = Field(default_factory=list)
    evidence_dossiers: list[EvidenceDossier] = Field(default_factory=list)
    critique_decision: CritiqueDecision | None = None
    agent_steps: list[AgentStep] = Field(default_factory=list)
    latency_ms: int | None = None
    total_latency_ms: int | None = None
    retrieval_latency_ms: int | None = None
    llm_call_count: int = 0
    retry_count: int = 0


class ReviewRunResponse(StrictModel):
    """Response returned after creating a review skeleton."""

    review_case: ReviewCase
    trace: RetrievalTrace
    result: ReviewResult
    case_path: Path
    trace_path: Path
    result_path: Path


class ReviewFailedResponse(StrictModel):
    """Minimal structured response for workflow failures."""

    status: Literal["review_failed"] = "review_failed"
    failed_node: str
    reason: str
    message: str
    attempts: int
    trace_id: str | None = None
