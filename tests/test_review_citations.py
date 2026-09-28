"""Tests for citation validation and grouping (Issue 8)."""

from law_agent.data.schemas import Chunk
from law_agent.review.citations import group_citations
from law_agent.review.schemas import (
    RetrievalHit,
    ReviewFacts,
)

# ---------------------------------------------------------------------------
# Helper: create RetrievalHit
# ---------------------------------------------------------------------------


def _hit(
    chunk_id: str = "c1",
    citation_role: str = "primary_legal_basis",
    can_cite: bool = True,
    title: str = "数据出境安全评估办法",
    text: str = "第四条　数据处理者向境外提供数据，应当申报数据出境安全评估。",
) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=chunk_id,
        doc_id="d1",
        source_id="s1",
        title=title,
        text=text,
        score=1.0,
        rank=0,
        retriever="hybrid",
        citation_role=citation_role,
        can_cite_clause=can_cite,
        source_url="u",
    )


# ---------------------------------------------------------------------------
# Demoted citations: usage must match the (demoted) group, not the original role
# ---------------------------------------------------------------------------


def test_demoted_citation_usage_is_implementation_reference() -> None:
    """A primary_legal_basis hit with can_cite_clause=False is demoted to the
    implementation_reference group. Its Citation.usage must reflect the demoted
    usage ("implementation_reference"), NOT the original role ("legal_basis").
    """

    hit = _hit(citation_role="primary_legal_basis", can_cite=False)

    groups, _violations = group_citations([hit], ReviewFacts(), {})

    # Should NOT appear in the legal_basis group
    legal_groups = [g for g in groups if g.usage == "legal_basis"]
    assert legal_groups == []

    # Should appear in the implementation_reference group
    impl_groups = [g for g in groups if g.usage == "implementation_reference"]
    assert len(impl_groups) == 1
    assert len(impl_groups[0].citations) == 1

    citation = impl_groups[0].citations[0]
    # The bug: usage says "legal_basis" even though it's in the
    # implementation_reference group. It must say "implementation_reference".
    assert citation.usage == "implementation_reference"
    assert citation.usage != "legal_basis"


def test_demoted_conditional_basis_usage_is_implementation_reference() -> None:
    """A conditional_local_basis hit with can_cite_clause=False is demoted to the
    implementation_reference group. Its Citation.usage must reflect the demoted
    usage ("implementation_reference"), NOT the original role ("conditional_basis").
    """

    hit = _hit(citation_role="conditional_local_basis", can_cite=False)

    groups, _violations = group_citations([hit], ReviewFacts(), {})

    # Should NOT appear in the conditional_basis group
    cond_groups = [g for g in groups if g.usage == "conditional_basis"]
    assert cond_groups == []

    # Should appear in the implementation_reference group
    impl_groups = [g for g in groups if g.usage == "implementation_reference"]
    assert len(impl_groups) == 1
    assert len(impl_groups[0].citations) == 1

    citation = impl_groups[0].citations[0]
    assert citation.usage == "implementation_reference"
    assert citation.usage != "conditional_basis"


def _negative_list_chunk() -> Chunk:
    return Chunk(
        chunk_id="c9",
        doc_id="d1",
        source_id="s1",
        title="中国（上海）自由贸易试验区数据出境管理清单（负面清单）（2025版）",
        text="再保险领域：需要通过数据出境安全评估的数据清单。",
        chunk_index=0,
        source_url="u",
        char_count=26,
        doc_type="guideline",
        citation_role="conditional_local_basis",
        applicable_region="CN-SH",
    )


def test_demoted_reference_states_its_nature_and_applicability_boundary() -> None:
    """降级为参考材料时，仍要说明材料性质与适用边界，而不是一句笼统免责。"""

    chunk = _negative_list_chunk()
    hit = _hit(chunk_id="c9", citation_role="conditional_local_basis", can_cite=False)

    groups, _violations = group_citations([hit], ReviewFacts(), {"c9": chunk})

    group = next(g for g in groups if g.usage == "implementation_reference")
    assert group.scope_note == "实施指南/管理清单，不作为条款级法律依据；仅适用于地区：CN-SH"


def _regional_chunk(chunk_id: str, region: str, subject: str) -> Chunk:
    return Chunk(
        chunk_id=chunk_id,
        doc_id=f"d_{chunk_id}",
        source_id="s1",
        title=f"{region}负面清单",
        text=f"{subject}领域：需要通过数据出境安全评估的数据清单。",
        chunk_index=0,
        source_url="u",
        char_count=26,
        doc_type="guideline",
        citation_role="conditional_local_basis",
        applicable_region=region,
        applicable_subjects=[subject],
    )


def test_group_scope_note_covers_every_region_in_the_group() -> None:
    """同组含北京、上海清单时，范围不能只显示第一条的地区。"""

    beijing = _regional_chunk("c1", "CN-BJ", "汽车行业")
    shanghai = _regional_chunk("c2", "CN-SH", "再保险")
    groups, _violations = group_citations(
        [
            _hit(chunk_id="c1", citation_role="conditional_local_basis", title="北京负面清单"),
            _hit(chunk_id="c2", citation_role="conditional_local_basis", title="上海负面清单"),
        ],
        ReviewFacts(),
        {"c1": beijing, "c2": shanghai},
    )

    group = next(g for g in groups if g.usage == "conditional_basis")
    assert group.scope_note == "仅适用于地区：CN-BJ、CN-SH"


def test_group_scope_note_says_when_only_part_of_the_group_is_restricted() -> None:
    """全国性规则与地方清单同组时，范围不能读成整组都受地区限制。"""

    national = Chunk(
        chunk_id="c1",
        doc_id="d1",
        source_id="s1",
        title="数据出境安全评估办法",
        text="第四条　数据处理者向境外提供数据，应当申报数据出境安全评估。",
        chunk_index=0,
        source_url="u",
        char_count=30,
        doc_type="guideline",
        citation_role="primary_legal_basis",
        applicable_region="CN",
    )
    shanghai = _regional_chunk("c2", "CN-SH", "再保险")
    groups, _violations = group_citations(
        [
            _hit(chunk_id="c1", citation_role="primary_legal_basis", can_cite=False),
            _hit(chunk_id="c2", citation_role="conditional_local_basis", can_cite=False),
        ],
        ReviewFacts(),
        {"c1": national, "c2": shanghai},
    )

    group = next(g for g in groups if g.usage == "implementation_reference")
    assert group.scope_note == (
        "实施指南/管理清单，不作为条款级法律依据；组内部分证据仅适用于地区：CN-SH"
    )


def test_group_citations_assigns_unique_case_local_refs() -> None:
    groups, violations = group_citations(
        [
            _hit(chunk_id="c1"),
            _hit(
                chunk_id="c2",
                title="个人信息保护法",
                text="第三十九条　个人信息处理者向境外提供个人信息。",
            ),
            _hit(
                chunk_id="c3",
                title="数据出境安全评估办法",
                text="第八条　应当评估出境活动的合法性。",
            ),
        ],
        ReviewFacts(),
        {},
    )

    refs = [citation.citation_ref for group in groups for citation in group.citations]
    assert violations == []
    assert refs == ["法源-01", "法源-02", "法源-03"]
    assert len(refs) == len(set(refs))
