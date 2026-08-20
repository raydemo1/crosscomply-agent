"""Pydantic schemas for LawAgent data governance artifacts."""

from __future__ import annotations

import ast
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

DocType = Literal[
    "law",
    "regulation",
    "policy",
    "faq",
    "guideline",
    "privacy_policy",
    "internal_policy",
    "case",
    "contract",
]

Authority = Literal[
    "national_law",
    "administrative_regulation",
    "ministry_policy",
    "local_regulation",
    "judicial_interpretation",
    "public_interpretation",
    "privacy_policy",
    "simulated_internal_policy",
    "unknown",
]

LawStatus = Literal["effective", "not_yet_effective", "amended", "repealed", "unknown"]
LibraryKind = Literal["legal", "internal_policy"]
InternalPolicyStatus = Literal["draft", "effective", "retired"]
ClauseCitationRole = Literal[
    "primary_legal_basis",
    "conditional_local_basis",
    "conditional_industry_basis",
    "implementation_reference",
    "interpretation_auxiliary",
]


def _split_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        items: list[str] = []
        for item in value:
            items.extend(_split_list(item))
        return items
    if isinstance(value, str):
        # Older CSV manifests may contain a Python-list representation,
        # possibly nested by repeated read/write cycles. Recover it before
        # applying the semicolon-delimited representation used by current files.
        if value.strip().startswith("[") and value.strip().endswith("]"):
            try:
                parsed = ast.literal_eval(value)
            except (SyntaxError, ValueError):
                parsed = None
            if isinstance(parsed, list):
                return _split_list(parsed)
        normalized = value.replace("；", ";")
        return [part.strip() for part in normalized.split(";") if part.strip()]
    return [str(value).strip()]


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return False
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "是", "include"}
    return bool(value)


class StrictModel(BaseModel):
    """Base model that rejects unknown fields."""

    model_config = ConfigDict(extra="forbid")


class SourceRecord(StrictModel):
    """A candidate source row before ingestion."""

    source_id: str
    library_kind: LibraryKind = "legal"
    title: str
    source_url: str
    download_url: str | None = None
    source_site: str
    doc_type: DocType
    authority: Authority = "unknown"
    law_status: LawStatus = "unknown"
    publish_date: str | None = None
    effective_date: str | None = None
    issuing_body: str | None = None
    owning_department: str | None = None
    internal_status: InternalPolicyStatus | None = None
    applicable_region: str = "CN"
    legal_domain: list[str] = Field(default_factory=list)
    applicable_subjects: list[str] = Field(default_factory=list)
    case_no: str | None = None
    court: str | None = None
    trial_instance: str | None = None
    contract_parties: list[str] = Field(default_factory=list)
    clause_type: str | None = None
    topic_tags: list[str] = Field(default_factory=list)
    language: Literal["zh", "en", "mixed"] = "zh"
    file_format: str = "html"
    include_in_mvp: bool = False
    review_note: str | None = None

    @field_validator(
        "topic_tags",
        "legal_domain",
        "applicable_subjects",
        "contract_parties",
        mode="before",
    )
    @classmethod
    def parse_string_lists(cls, value: Any) -> list[str]:
        return _split_list(value)

    @field_validator("include_in_mvp", mode="before")
    @classmethod
    def parse_include_in_mvp(cls, value: Any) -> bool:
        return _to_bool(value)

    @field_validator("internal_status", mode="before")
    @classmethod
    def parse_internal_status(cls, value: Any) -> Any:
        return None if value in (None, "") else value


class Attachment(StrictModel):
    """A linked attachment discovered during ingestion."""

    attachment_id: str
    url: str
    media_type: str | None = None
    local_path: str | None = None


class IngestMeta(StrictModel):
    """Trace metadata for how a document was fetched and parsed."""

    fetched_at: str
    parser: str
    parser_version: str


class Document(StrictModel):
    """Normalized document before cleaning and enrichment."""

    doc_id: str
    source_id: str
    library_kind: LibraryKind = "legal"
    title: str
    source_url: str
    download_url: str | None = None
    source_site: str
    doc_type: DocType
    authority: Authority = "unknown"
    law_status: LawStatus = "unknown"
    publish_date: str | None = None
    effective_date: str | None = None
    issuing_body: str | None = None
    owning_department: str | None = None
    internal_status: InternalPolicyStatus | None = None
    language: Literal["zh", "en", "mixed"] = "zh"
    applicable_region: str = "CN"
    legal_domain: list[str] = Field(default_factory=list)
    applicable_subjects: list[str] = Field(default_factory=list)
    case_no: str | None = None
    court: str | None = None
    trial_instance: str | None = None
    contract_parties: list[str] = Field(default_factory=list)
    clause_type: str | None = None
    topic_tags: list[str] = Field(default_factory=list)
    raw_format: str = "html"
    text: str
    attachments: list[Attachment] = Field(default_factory=list)
    ingest_meta: IngestMeta


class CleanedDocument(Document):
    """Document after deterministic cleaning."""

    cleaning_version: str = "0.1.0"
    cleaning_rule_hits: dict[str, int] = Field(default_factory=dict)


class EnrichmentMeta(StrictModel):
    """Trace metadata for LLM or rule-based enrichment."""

    model: str
    prompt_version: str
    generated_at: str


class Enrichment(StrictModel):
    """Semantic fields generated for retrieval and evidence checks."""

    summary: str
    keywords: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    topic_tags: list[str] = Field(default_factory=list)
    applicable_subjects: list[str] = Field(default_factory=list)
    authority_level: Authority = "unknown"
    risk_tags: list[str] = Field(default_factory=list)
    effective_status: LawStatus = "unknown"
    enrichment_meta: EnrichmentMeta


class EnrichedDocument(CleanedDocument):
    """Cleaned document plus semantic enrichment fields."""

    enrichment: Enrichment


class Chunk(StrictModel):
    """Retrieval chunk with parent document traceability."""

    chunk_id: str
    doc_id: str
    source_id: str
    library_kind: LibraryKind = "legal"
    title: str
    text: str
    chunk_index: int
    doc_type: DocType = "law"
    heading_path: list[str] = Field(default_factory=list)
    article_no: str | None = None
    paragraph_no: str | None = None
    item_no: str | None = None
    citation_label: str | None = None
    citation_role: ClauseCitationRole = "interpretation_auxiliary"
    can_cite_clause: bool = False
    prev_chunk_id: str | None = None
    next_chunk_id: str | None = None
    authority: Authority = "unknown"
    law_status: LawStatus = "unknown"
    publish_date: str | None = None
    effective_date: str | None = None
    source_url: str
    applicable_region: str = "CN"
    issuing_body: str | None = None
    owning_department: str | None = None
    internal_status: InternalPolicyStatus | None = None
    legal_domain: list[str] = Field(default_factory=list)
    applicable_subjects: list[str] = Field(default_factory=list)
    case_no: str | None = None
    court: str | None = None
    trial_instance: str | None = None
    contract_parties: list[str] = Field(default_factory=list)
    clause_type: str | None = None
    topic_tags: list[str] = Field(default_factory=list)
    char_count: int

    @field_validator("char_count")
    @classmethod
    def positive_char_count(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("char_count must be positive")
        return value
