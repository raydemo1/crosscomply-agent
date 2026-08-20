"""Request models owned by the HTTP adapters."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

from law_agent.config import RerankMode
from law_agent.data.schemas import InternalPolicyStatus, LibraryKind
from law_agent.review.evalset.cases import EvalSuite
from law_agent.review.evalset.runner import ReviewEvalMode
from law_agent.review.rules import ComplianceFacts
from law_agent.review.service import ReviewMode
from law_agent.review.user_admin import UserRole


class KnowledgeDeletePreviewRequest(BaseModel):
    library_kind: LibraryKind
    source_ids: list[str] = Field(..., min_length=1, max_length=100)


class KnowledgeDeleteJobRequest(BaseModel):
    token: str = Field(..., min_length=16)
    confirmation: str = Field(..., min_length=1)


class KnowledgeImportCommitRequest(BaseModel):
    preview_id: str = Field(..., min_length=16)


class KnowledgeMetadataUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=240)
    source_url: str | None = None
    source_site: str | None = None
    doc_type: Literal[
        "law",
        "regulation",
        "policy",
        "faq",
        "guideline",
        "privacy_policy",
        "internal_policy",
        "case",
        "contract",
    ] | None = None
    authority: Literal[
        "national_law",
        "administrative_regulation",
        "ministry_policy",
        "local_regulation",
        "judicial_interpretation",
        "public_interpretation",
        "privacy_policy",
        "simulated_internal_policy",
        "unknown",
    ] | None = None
    law_status: Literal["effective", "not_yet_effective", "amended", "repealed", "unknown"] | None = None
    publish_date: str | None = None
    effective_date: str | None = None
    issuing_body: str | None = None
    owning_department: str | None = None
    internal_status: InternalPolicyStatus | None = None
    topic_tags: list[str] | None = None


class KnowledgeRestoreRequest(BaseModel):
    library_kind: LibraryKind


class LoginRequest(BaseModel):
    username: str = Field(..., min_length=1)
    password: str = Field(..., min_length=1)


class HealthResponse(BaseModel):
    status: str = "ok"
    llm: dict[str, object] = Field(default_factory=dict)
    services: dict[str, object] = Field(default_factory=dict)
    corpus: dict[str, object] = Field(default_factory=dict)


class EvalRunRequest(BaseModel):
    chunks_path: str | None = None
    review_mode: ReviewEvalMode = "llm"
    top_k: int = Field(default=10, ge=1, le=100)
    max_workers: int = Field(default=4, ge=1, le=16)
    rerank_mode: RerankMode = "off"
    suite: EvalSuite = "full"


class EvalJobResponse(BaseModel):
    job_id: str | None = None
    status: Literal["idle", "running", "succeeded", "failed"] = "idle"
    message: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


class IntakePayload(BaseModel):
    business_activity: str = ""
    data_types: list[str] = Field(default_factory=list)
    sensitive_personal_info: bool | None = None
    cross_border_transfer: bool | None = None
    important_data_status: Literal["unknown", "not_important", "important", "under_review"] = "unknown"
    ciio_status: Literal["unknown", "not_ciio", "ciio", "under_review"] = "unknown"
    annual_non_sensitive_count: str = ""
    annual_sensitive_count: str = ""
    overseas_recipient: str = ""
    destination_region: str = ""
    processing_purpose: str = ""
    transfer_mechanism: str = ""
    vendor_name: str = ""
    contract_status: str = ""
    legal_basis_or_consent: str = ""
    notes: str = ""


class CaseCreateRequest(BaseModel):
    title: str | None = None
    question: str = Field(..., min_length=1)
    material_text: str = Field(..., min_length=1)
    material_source: str | None = None
    intake: IntakePayload = Field(default_factory=IntakePayload)
    review_mode: ReviewMode = "llm"
    rerank_mode: RerankMode = "off"

    @field_validator("question", "material_text")
    @classmethod
    def must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value.strip()


class CaseUpdateRequest(BaseModel):
    title: str | None = None
    question: str | None = None
    material_text: str | None = None
    intake: IntakePayload | None = None
    facts_confirmed: bool | None = None
    owner_id: str | None = None


class CaseStatusRequest(BaseModel):
    status: Literal[
        "draft",
        "needs_info",
        "pending_review",
        "review_running",
        "pending_feishu_approval",
        "approved",
        "conditionally_approved",
        "rejected",
        "run_failed",
    ]
    note: str = ""


class MaterialSnapshotRequest(BaseModel):
    version_ids: list[str] = Field(..., min_length=1)
    facts: ComplianceFacts


class RemediationEvidenceRequest(BaseModel):
    kind: Literal["case_material", "file", "link"]
    label: str = Field(..., min_length=1)
    uri: str | None = None
    object_key: str | None = None
    content_type: str | None = None
    sha256: str | None = None
    byte_size: int | None = Field(default=None, ge=0)


class RemediationTaskRequest(BaseModel):
    title: str = Field(..., min_length=1)
    description: str = ""
    acceptance_criteria: str = ""
    source_recommendation_index: int | None = Field(default=None, ge=0)
    source_recommendation: str | None = None
    assignee_id: str | None = None
    priority: Literal["high", "medium", "low"] = "medium"
    due_date: str | None = None


class RemediationPlanRequest(BaseModel):
    tasks: list[RemediationTaskRequest] = Field(default_factory=list, max_length=30)
    no_remediation_reason: str | None = None


class RemediationTaskUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1)
    description: str | None = None
    acceptance_criteria: str | None = None
    assignee_id: str | None = None
    priority: Literal["high", "medium", "low"] | None = None
    due_date: str | None = None
    expected_version: int | None = Field(default=None, ge=1)


class RemediationSubmissionRequest(BaseModel):
    note: str = Field(..., min_length=1)
    evidence: list[RemediationEvidenceRequest] = Field(default_factory=list, max_length=5)


class RemediationSubmissionReviewRequest(BaseModel):
    decision: Literal["accepted", "rejected"]
    review_note: str | None = None


class FeedbackRequest(BaseModel):
    conclusion_useful: bool | None = None
    missing_sources: str = ""
    notes: str = ""
    citation_verdicts: dict[str, str] = Field(default_factory=dict)


class UserCreateRequest(BaseModel):
    username: str = Field(..., min_length=1)
    display_name: str = Field(..., min_length=1)
    password: str = Field(..., min_length=12)
    role: UserRole


class UserStateRequest(BaseModel):
    active: bool


class UserPasswordResetRequest(BaseModel):
    password: str = Field(..., min_length=12)


class UserRoleRequest(BaseModel):
    role: UserRole


class CaseTemplateCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    question: str = Field(..., min_length=1, max_length=4000)
    intake: dict[str, object] = Field(default_factory=dict)
    review_mode: Literal["llm", "multi_agent"] = "llm"
    rerank_mode: Literal["off", "embedding"] = "off"

    @field_validator("name", "question")
    @classmethod
    def template_text_must_not_be_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("模板名称和审查问题不能为空")
        return value.strip()


class CaseTemplateUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    question: str | None = Field(default=None, min_length=1, max_length=4000)
    intake: dict[str, object] | None = None
    review_mode: Literal["llm", "multi_agent"] | None = None
    rerank_mode: Literal["off", "embedding"] | None = None


__all__ = [
    "CaseCreateRequest",
    "CaseStatusRequest",
    "CaseTemplateCreateRequest",
    "CaseTemplateUpdateRequest",
    "CaseUpdateRequest",
    "EvalJobResponse",
    "EvalRunRequest",
    "FeedbackRequest",
    "HealthResponse",
    "IntakePayload",
    "KnowledgeDeleteJobRequest",
    "KnowledgeDeletePreviewRequest",
    "KnowledgeImportCommitRequest",
    "KnowledgeMetadataUpdateRequest",
    "KnowledgeRestoreRequest",
    "LoginRequest",
    "MaterialSnapshotRequest",
    "RemediationEvidenceRequest",
    "RemediationPlanRequest",
    "RemediationSubmissionRequest",
    "RemediationSubmissionReviewRequest",
    "RemediationTaskRequest",
    "RemediationTaskUpdateRequest",
    "UserCreateRequest",
    "UserPasswordResetRequest",
    "UserRoleRequest",
    "UserStateRequest",
]
