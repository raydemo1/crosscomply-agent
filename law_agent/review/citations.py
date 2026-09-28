"""Citation validation and grouping for governed review results.

Issue 8: Validate that clause-level citations only use ``can_cite_clause=True``
evidence, and group citations by usage category:
- ``legal_basis``: primary_legal_basis with can_cite_clause
- ``conditional_basis``: conditional_local_basis / conditional_industry_basis
- ``implementation_reference``: implementation_reference (TC260/GB/T etc.)
- ``policy_explanation``: interpretation_auxiliary (official Q&A etc.)

``can_cite_clause`` is decided by the instrument's own nature (see
:mod:`law_agent.data.citation_policy`), not by the citation role alone, so a
verified local regulation or industry rule keeps its clause-level effect while
guidelines, standards, policy Q&A and negative lists stay references. Whether a
basis *applies* to this case is not a citation gate: the group and its scope
note carry that boundary, and retrieval boosts only re-rank.
"""

from __future__ import annotations

from collections.abc import Sequence

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


# What a non-normative material *is*, so a demoted citation still states its
# real nature instead of being flattened into a generic "参考材料".
_REFERENCE_NATURE: dict[str, str] = {
    "guideline": "实施指南/管理清单",
    "standard": "标准文本",
    "faq": "政策问答口径",
    "policy": "政策文件",
    "contract": "合同范本",
}


def _applicability_note(chunks: Sequence[Chunk]) -> str | None:
    """The applicability boundary (适用性) the evidence in one group carries.

    Read over the whole group, never the first chunk: a group holding a Beijing
    list and a Shanghai list showed "仅适用于地区：北京" while the Shanghai list
    was grouped under the very same note. When part of the group carries no
    boundary at all — a nationwide rule beside a district list — the note says
    so, instead of reading as if the whole group were restricted.
    """

    regions = sorted(
        {
            chunk.applicable_region
            for chunk in chunks
            if chunk.applicable_region and chunk.applicable_region != "CN"
        }
    )
    if regions:
        note = f"仅适用于地区：{'、'.join(regions)}"
        unrestricted = any(
            not chunk.applicable_region or chunk.applicable_region == "CN" for chunk in chunks
        )
        return f"组内部分证据{note}" if unrestricted else note
    subjects = sorted({subject for chunk in chunks for subject in chunk.applicable_subjects})
    if subjects:
        shown = "、".join(subjects[:3])
        return f"仅适用于：{shown}" + (f" 等 {len(subjects)} 类" if len(subjects) > 3 else "")
    return None


def _build_scope_note(
    usage: CitationUsage, facts: ReviewFacts, chunks: Sequence[Chunk]
) -> str | None:
    """State the material nature and applicability boundary of a citation group.

    ``conditional_basis`` already conveys the boundary. A group demoted out of
    the clause-level ones keeps both the nature of its material (指南/标准/
    问答/清单…) and that boundary, so the reader sees what the evidence is and
    where it applies rather than a bare "not usable" disclaimer.
    """

    scope = _applicability_note(chunks)

    if usage == "conditional_basis":
        return scope
    if usage == "implementation_reference":
        natures = sorted({_REFERENCE_NATURE.get(chunk.doc_type, "参考材料") for chunk in chunks})
        note = f"{'、'.join(natures or ['参考材料'])}，不作为条款级法律依据"
        return f"{note}；{scope}" if scope else note
    if usage == "policy_explanation":
        note = "政策问答口径，不作为条款级法律依据"
        return f"{note}；{scope}" if scope else note
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

        # The note describes the whole group, so it is built from every chunk
        # the group holds, not from whichever one happened to be first.
        scope_note = _build_scope_note(
            usage,
            facts,
            [
                chunks_by_id[hit.chunk_id]
                for hit, hit_usage in hits_with_usage
                if hit_usage == usage and hit.chunk_id in chunks_by_id
            ],
        )

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
