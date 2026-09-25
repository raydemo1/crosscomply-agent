"""Schemas for material-driven compliance review runs."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator

from law_agent.data.schemas import Authority, ClauseCitationRole, DocType, LawStatus, StrictModel

ReviewInputMode = Literal["pasted_text", "uploaded_file"]
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
IssueKind = Literal["material_conflict", "missing_information", "legal_gap"]


class ReviewFacts(StrictModel):
    """Structured facts extracted from user material."""

    business_activity: str | None = None
    data_types: list[str] = Field(default_factory=list)
    contains_personal_information: bool | None = None
    sensitive_personal_info: bool | None = None
    cross_border_transfer: bool | None = None
    overseas_recipient: str | None = None
    processing_purpose: str | None = None
    legal_basis_or_consent: str | None = None
    industry: str | None = None
    regions: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    as_of_date: date | None = None

    @field_validator("as_of_date", mode="before")
    @classmethod
    def parse_as_of_date(cls, value: object) -> object:
        if isinstance(value, str):
            return date.fromisoformat(value)
        return value

    @field_validator(
        "business_activity",
        "overseas_recipient",
        "processing_purpose",
        "legal_basis_or_consent",
        "industry",
        mode="before",
    )
    @classmethod
    def normalize_semantic_null_strings(cls, value: object) -> object:
        if isinstance(value, str) and value.strip().casefold() in _SEMANTIC_NULL_STRINGS:
            return None
        return value

    @field_validator("regions", mode="before")
    @classmethod
    def normalize_regions(cls, value: object) -> object:
        # Drop blank entries and duplicates, but keep whatever the extractor
        # emitted: a national value such as "CN" must stay visible so that
        # extraction quality stays measurable downstream.
        if not isinstance(value, list):
            return value
        cleaned: list[object] = []
        for item in value:
            if isinstance(item, str):
                item = item.strip()
                if not item:
                    continue
            if item not in cleaned:
                cleaned.append(item)
        return cleaned


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


class MaterialEvidenceRef(StrictModel):
    """One verified excerpt from a frozen material version.

    ``start_offset``/``end_offset`` are computed by the finalizer after the
    quote is located in the frozen ``parsed_text``; the model never produces
    them.
    """

    material_version_id: str
    logical_name: str
    filename: str
    version_number: int
    quote: str
    start_offset: int
    end_offset: int


class ReviewIssue(StrictModel):
    """One investigation finding surfaced to the case detail page.

    ``kind`` decides which evidence is mandatory:

    * ``material_conflict`` requires at least two distinct grounded material
      excerpts;
    * ``legal_gap`` requires both a grounded material excerpt and a citable
      legal chunk from the current evidence set;
    * ``missing_information`` requires at least one unresolved unknown and may
      carry context excerpts.
    """

    id: str
    kind: IssueKind
    title: str
    finding: str
    material_evidence: list[MaterialEvidenceRef]
    supporting_chunk_ids: list[str]
    supporting_citation_refs: list[str]
    unknowns: list[str]
    recommended_action: str


class ReviewResult(StrictModel):
    """Structured review result produced from facts and evidence."""

    review_result_id: str
    review_case_id: str
    trace_id: str
    risk_level: RiskLevel
    decision_summary: str = Field(min_length=40, max_length=240)
    conclusion: str
    review_facts: ReviewFacts
    trigger_reasons: list[str] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    risk_boundaries: list[str] = Field(default_factory=list)
    claims: list[GroundedClaim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    applicable_evidence: list[CitationGroup] = Field(default_factory=list)
    issues: list[ReviewIssue] = Field(default_factory=list)


class ReviewCase(StrictModel):
    """A material-driven compliance review case."""

    review_case_id: str
    created_at: str
    question: str
    material: MaterialRecord
    review_facts: ReviewFacts
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
