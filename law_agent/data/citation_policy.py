"""Citation policy for governed legal sources and concrete articles."""

from __future__ import annotations

from law_agent.data.schemas import ClauseCitationRole, Document, SourceRecord

FRONTEND_DIRECT_REFERENCE_SOURCE_IDS = {
    "flk_npc_ff808181927f0e7b0192949a1da4355d",
    "flk_npc_ff8081817b6472a3017b656cc2040044",
    "flk_npc_ff80818179f5e0800179f885c7e70392",
    "flk_npc_021e7d7684474107b8f3febbb1c4f8b5",
    "cac_data_export_security_assessment_measures_2022",
    "cac_personal_info_export_standard_contract_measures_2023",
    "cac_personal_info_export_standard_contract_filing_guide_v2_2024",
    "cac_personal_info_export_standard_contract_template_2023",
    "cac_data_export_security_assessment_filing_guide_v3_2025",
}


def citation_role_for_source(source: SourceRecord | Document) -> ClauseCitationRole:
    """Return the source's governed citation role, independent of its ID."""
    return source.citation_role


def can_cite_clause(source: SourceRecord | Document) -> bool:
    """Whether this governed source can support clause-level legal claims."""
    return source.library_kind == "legal" and citation_role_for_source(source) == "primary_legal_basis"


def can_cite_clause_chunk(source: SourceRecord | Document, article_no: str | None) -> bool:
    """Require both primary legal status and a concrete article number."""
    return can_cite_clause(source) and bool(article_no)


def default_retrievable_for_source(source: SourceRecord) -> bool:
    """All governed sources are eligible for normal retrieval."""
    return True


def frontend_direct_reference_for_source(source_id: str) -> bool:
    """Whether a source is in the curated frontend quick-reference list."""
    return source_id in FRONTEND_DIRECT_REFERENCE_SOURCE_IDS
