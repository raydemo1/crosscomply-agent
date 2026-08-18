"""Citation validation and grouping for governed review results.

Issue 8: Validate that clause-level citations only use ``can_cite_clause=True``
evidence, and group citations by usage category:
- ``legal_basis``: primary_legal_basis with can_cite_clause
- ``conditional_basis``: conditional_local_basis / conditional_industry_basis
- ``implementation_reference``: implementation_reference (TC260/GB/T etc.)
- ``policy_explanation``: interpretation_auxiliary (official Q&A etc.)

Local and industry evidence includes scope wording to make applicability
explicit.
"""

from __future__ import annotations

from law_agent.data.schemas import Chunk
from law_agent.review.schemas import (
    Citation,
    CitationGroup,
    CitationUsage,
    RetrievalHit,
    ReviewFacts,
)

# ---------------------------------------------------------------------------
# Citation validation
# ---------------------------------------------------------------------------


class CitationValidationError(Exception):
    """Raised when a citation violates governance rules."""


def validate_citation(hit: RetrievalHit, usage: CitationUsage) -> list[str]:
    """Validate a single hit for a given usage. Returns violation messages.

    Rules:
    - ``legal_basis`` usage requires ``can_cite_clause=True``
    - ``conditional_basis`` usage requires ``can_cite_clause=True``
    - ``implementation_reference`` and ``policy_explanation`` allow
      ``can_cite_clause=False``
    """

    violations: list[str] = []

    if usage in ("legal_basis", "conditional_basis") and not hit.can_cite_clause:
        violations.append(
            f"citation_role={hit.citation_role} usage={usage} requires "
            f"can_cite_clause=True, but chunk {hit.chunk_id} has can_cite_clause=False"
        )

    return violations


def validate_citations(hits_with_usage: list[tuple[RetrievalHit, CitationUsage]]) -> list[str]:
    """Validate all citations. Returns list of violation messages (empty if valid)."""

    violations: list[str] = []
    for hit, usage in hits_with_usage:
        violations.extend(validate_citation(hit, usage))
    return violations


# ---------------------------------------------------------------------------
# Citation grouping
# ---------------------------------------------------------------------------


def _determine_usage(hit: RetrievalHit) -> CitationUsage:
    """Determine the citation usage category from the hit's citation_role."""

    if hit.citation_role == "primary_legal_basis":
        return "legal_basis"
    if hit.citation_role in ("conditional_local_basis", "conditional_industry_basis"):
        return "conditional_basis"
    if hit.citation_role == "implementation_reference":
        return "implementation_reference"
    if hit.citation_role == "interpretation_auxiliary":
        return "policy_explanation"
    # Fallback: treat unknown roles as implementation_reference
    return "implementation_reference"


def _build_citation(
    hit: RetrievalHit,
    chunk: Chunk | None = None,
    usage: CitationUsage | None = None,
    full_article_text: str | None = None,
) -> Citation:
    """Build a Citation from a RetrievalHit, optionally enriched with chunk data.

    ``usage`` lets the caller pass the final (possibly demoted) usage category.
    When omitted (None), it falls back to ``_determine_usage(hit)`` so that any
    direct callers keep the previous behavior.
    """

    citation_label = hit.citation_label
    if chunk is not None:
        citation_label = chunk.citation_label or citation_label

    if usage is None:
        usage = _determine_usage(hit)

    return Citation(
        source_id=hit.source_id,
        chunk_id=hit.chunk_id,
        title=hit.title,
        source_url=hit.source_url,
        citation_role=hit.citation_role,
        can_cite_clause=hit.can_cite_clause,
        usage=usage,
        citation_label=citation_label,
        article_no=chunk.article_no if chunk is not None else hit.article_no,
        full_article_text=full_article_text or hit.full_article_text,
        doc_type=chunk.doc_type if chunk is not None else hit.doc_type,
        authority=chunk.authority if chunk is not None else hit.authority,
        law_status=chunk.law_status if chunk is not None else hit.law_status,
        publish_date=chunk.publish_date if chunk is not None else hit.publish_date,
        effective_date=chunk.effective_date if chunk is not None else hit.effective_date,
        issuing_body=chunk.issuing_body if chunk is not None else hit.issuing_body,
        heading_path=chunk.heading_path if chunk is not None else hit.heading_path,
    )


def _full_article_text(
    hit: RetrievalHit,
    chunks_by_id: dict[str, Chunk],
) -> str | None:
    """Assemble only the cited article, never adjacent article chunks."""

    chunk = chunks_by_id.get(hit.chunk_id)
    article_no = chunk.article_no if chunk is not None else hit.article_no
    if not article_no or not hit.can_cite_clause:
        return None

    article_chunks = [
        candidate
        for candidate in chunks_by_id.values()
        if candidate.source_id == hit.source_id and candidate.article_no == article_no
    ]
    if not article_chunks:
        return None

    texts: list[str] = []
    seen: set[str] = set()
    for candidate in sorted(article_chunks, key=lambda item: item.chunk_index):
        text = candidate.text.strip()
        if text and text not in seen:
            texts.append(text)
            seen.add(text)
    return "\n".join(texts) or None


def _build_scope_note(usage: CitationUsage, facts: ReviewFacts, chunk: Chunk | None) -> str | None:
    """Build scope wording for conditional basis citations."""

    if usage == "conditional_basis" and chunk is not None:
        if chunk.applicable_region and chunk.applicable_region != "CN":
            return f"仅适用于地区：{chunk.applicable_region}"
        if chunk.applicable_subjects:
            return f"仅适用于：{', '.join(chunk.applicable_subjects[:3])}"
    if usage == "implementation_reference":
        return "参考标准/实施指南，不作为条款级法律依据"
    if usage == "policy_explanation":
        return "政策口径补充，不作为条款级法律依据"
    return None


def group_citations(
    hits: list[RetrievalHit],
    facts: ReviewFacts,
    chunks_by_id: dict[str, Chunk] | None = None,
) -> tuple[list[CitationGroup], list[str]]:
    """Group hits into citation groups by usage category.

    Returns:
        - List of CitationGroup (non-empty groups only)
        - List of validation violation messages (empty if all valid)

    Clause-level citations (legal_basis, conditional_basis) that fail
    validation (can_cite_clause=False) are demoted to implementation_reference
    rather than discarded, so the evidence is still visible but not
    presented as clause-level legal basis.
    """

    if chunks_by_id is None:
        chunks_by_id = {}

    # First pass: determine usage and validate
    hits_with_usage: list[tuple[RetrievalHit, CitationUsage]] = []
    demoted_hits: list[RetrievalHit] = []

    for hit in hits:
        usage = _determine_usage(hit)
        violations = validate_citation(hit, usage)
        if violations:
            # Demote to implementation_reference
            demoted_hits.append(hit)
            hits_with_usage.append((hit, "implementation_reference"))
        else:
            hits_with_usage.append((hit, usage))

    all_violations = validate_citations(hits_with_usage)

    # Second pass: build citations and group
    groups: dict[CitationUsage, list[Citation]] = {
        "legal_basis": [],
        "conditional_basis": [],
        "implementation_reference": [],
        "policy_explanation": [],
    }

    for hit, usage in hits_with_usage:
        chunk = chunks_by_id.get(hit.chunk_id)
        citation = _build_citation(
            hit,
            chunk,
            usage,
            _full_article_text(hit, chunks_by_id),
        )
        groups[usage].append(citation)

    # Build CitationGroup list with scope notes
    result_groups: list[CitationGroup] = []
    for usage in (
        "legal_basis",
        "conditional_basis",
        "implementation_reference",
        "policy_explanation",
    ):
        citations = groups[usage]
        if not citations:
            continue

        # Build scope note from first citation's chunk
        scope_note = None
        if citations:
            first_hit = next((h for h, u in hits_with_usage if u == usage), None)
            if first_hit:
                chunk = chunks_by_id.get(first_hit.chunk_id)
                scope_note = _build_scope_note(usage, facts, chunk)

        result_groups.append(
            CitationGroup(
                usage=usage,
                citations=citations,
                scope_note=scope_note,
            )
        )

    citation_index = 1
    numbered_groups: list[CitationGroup] = []
    for group in result_groups:
        numbered = [
            citation.model_copy(update={"citation_ref": f"法源-{citation_index + offset:02d}"})
            for offset, citation in enumerate(group.citations)
        ]
        citation_index += len(numbered)
        numbered_groups.append(group.model_copy(update={"citations": numbered}))

    return numbered_groups, all_violations


# ---------------------------------------------------------------------------
# Summary helpers
# ---------------------------------------------------------------------------


def count_citations_by_usage(groups: list[CitationGroup]) -> dict[str, int]:
    """Count citations per usage category."""

    counts: dict[str, int] = {}
    for group in groups:
        counts[group.usage] = len(group.citations)
    return counts
