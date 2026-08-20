"""Tests for citation validation used by LLM-owned result building."""

from law_agent.review.citations import (
    count_citations_by_usage,
    group_citations,
    validate_citation,
)
from law_agent.review.result_builder import inject_citation_markers
from law_agent.review.schemas import GroundedClaim, RetrievalHit, ReviewFacts


def _hit(
    chunk_id: str = "c1",
    citation_role: str = "primary_legal_basis",
    can_cite: bool = True,
    title: str = "数据出境安全评估办法",
    text: str = "第四条　数据处理者向境外提供数据，应当申报数据出境安全评估。",
    source_id: str = "s1",
) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=chunk_id,
        doc_id=f"d_{chunk_id}",
        source_id=source_id,
        title=title,
        text=text,
        score=1.0,
        rank=0,
        retriever="elasticsearch",
        citation_role=citation_role,
        can_cite_clause=can_cite,
        source_url="https://example.com",
    )


def test_validate_legal_basis_requires_can_cite_clause() -> None:
    assert validate_citation(_hit(can_cite=False), "legal_basis") != []


def test_validate_legal_basis_passes_with_can_cite() -> None:
    assert validate_citation(_hit(can_cite=True), "legal_basis") == []


def test_group_citations_demotes_non_citable_legal_basis() -> None:
    groups, violations = group_citations(
        [_hit(can_cite=False)],
        ReviewFacts(),
    )

    assert violations == []  # demotion happens before validation, so no violations
    assert groups[0].usage == "implementation_reference"


def test_count_citations_by_usage() -> None:
    groups, _ = group_citations(
        [
            _hit(chunk_id="c1", can_cite=True),
            _hit(
                chunk_id="c2",
                citation_role="interpretation_auxiliary",
                can_cite=False,
            ),
        ],
        ReviewFacts(),
    )

    counts = count_citations_by_usage(groups)
    assert counts["legal_basis"] == 1
    assert counts["policy_explanation"] == 1


def test_inject_citation_markers_uses_paragraph_end_fallback() -> None:
    report = (
        "### 关键法律依据\n"
        "依据《数据出境安全评估办法》**第四条**，应当申报数据出境安全评估。\n\n"
        "### 风险判断\n"
        "当前业务属于个人信息出境，需先完成评估。"
    )
    claims = [
        GroundedClaim(
            text="依据《数据出境安全评估办法》第四条，应当申报数据出境安全评估。",
            supporting_chunk_ids=["c1"],
        ),
        GroundedClaim(
            text="当前业务属于个人信息出境，需先完成评估。",
            supporting_chunk_ids=["c2"],
        ),
    ]
    hits = [
        _hit(
            chunk_id="c1",
            text="第四条 数据处理者应当申报数据出境安全评估。",
        ).model_copy(update={"article_no": "第四条", "citation_label": "数据出境安全评估办法 第四条"}),
        _hit(
            chunk_id="c2",
            text="第八条 个人信息出境应当履行保护义务。",
        ).model_copy(update={"article_no": "第八条", "citation_label": "数据出境安全评估办法 第八条"}),
    ]

    marked = inject_citation_markers(
        report,
        claims,
        hits,
        {"c1": "法源-01", "c2": "法源-02"},
    )

    assert "**第四条**<sup" in marked
    assert "需先完成评估。<sup" in marked
    assert 'data-citation-ref="法源-02"' in marked
    assert "引用说明" not in marked
