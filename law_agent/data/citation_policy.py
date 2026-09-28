"""Citation policy for governed legal sources and concrete articles.

Three questions are answered separately, because conflating them either
over-grants authority or silently bans a usable clause:

1. **效力** — can this instrument itself support a clause-level legal claim?
   :func:`has_legal_effect`.  A local regulation, an administrative regulation
   or a judicial interpretation all can, whatever their ``citation_role``
   says: a ``conditional_local_basis`` role records *where* a norm applies, not
   whether it is a norm.  A guideline, a standard, a policy Q&A or a negative
   list cannot, and is not given that effect just because it sits in the legal
   library.
2. **条款定位** — does the chunk point at a concrete clause?
   :func:`has_clause_locator`.
3. **适用性** — does the basis apply to *this* case (region, industry, date)?
   That is not a citation gate at all: it is judged in
   :mod:`law_agent.review.retrieval.boosts` (soft ranking) and stated in the
   citation's scope note, so a locally-scoped norm keeps its clause-level
   effect while its boundary stays visible.
"""

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

# The document kinds that are themselves legal norms regardless of how widely
# they apply. Everything else in the legal library (guideline, policy, faq,
# standard, contract …) stays a reference: it may be cited for its content,
# with its nature and scope stated, but never as a clause-level legal basis.
CLAUSE_LEVEL_DOC_TYPES = frozenset({"law", "regulation", "judicial_interpretation"})

# Roles that narrow *where* a norm applies. They do not strip it of
# clause-level effect — that is what these roles mean.
CONDITIONAL_ROLES = frozenset({"conditional_local_basis", "conditional_industry_basis"})


def citation_role_for_source(source: SourceRecord | Document) -> ClauseCitationRole:
    """Return the source's governed citation role, independent of its ID."""
    return source.citation_role


def has_legal_effect(source: SourceRecord | Document) -> bool:
    """Whether the instrument itself can support clause-level legal claims.

    Judged by what the instrument *is*, not by which role it plays: a verified
    regulation keeps its clause-level effect even when its role is
    ``conditional_local_basis``/``conditional_industry_basis`` (that role only
    narrows where it applies).
    """

    if source.library_kind != "legal":
        return False
    role = citation_role_for_source(source)
    if role == "primary_legal_basis":
        return True
    # A conditional role narrows where a norm applies; it does not turn a real
    # regulation into a non-norm. An auxiliary role (政策解读/理解与适用) does
    # stay auxiliary, so the instrument kind is only consulted for conditional
    # roles.
    return role in CONDITIONAL_ROLES and source.doc_type in CLAUSE_LEVEL_DOC_TYPES


def has_clause_locator(article_no: str | None) -> bool:
    """Whether the chunk points at a concrete clause."""

    return bool(article_no and article_no.strip())


def can_cite_clause(source: SourceRecord | Document) -> bool:
    """Whether this governed source has clause-level effect (效力)."""

    return has_legal_effect(source)


def can_cite_clause_chunk(source: SourceRecord | Document, article_no: str | None) -> bool:
    """Require both clause-level effect (效力) and a concrete locator (定位)."""

    return has_legal_effect(source) and has_clause_locator(article_no)


def default_retrievable_for_source(source: SourceRecord) -> bool:
    """All governed sources are eligible for normal retrieval."""
    return True


def frontend_direct_reference_for_source(source_id: str) -> bool:
    """Whether a source is in the curated frontend quick-reference list."""
    return source_id in FRONTEND_DIRECT_REFERENCE_SOURCE_IDS
