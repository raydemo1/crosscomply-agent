"""Tests for evidence self-check and controlled second retrieval (Issue 7)."""

from law_agent.review.evidence import (
    build_second_retrieval_plan,
    evaluate_after_second_retrieval,
    run_self_check,
)
from law_agent.review.schemas import (
    EvidenceIssue,
    RetrievalHit,
    ReviewFacts,
)
from tests.test_review_retrieval_keyword import _make_chunk

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
# Self-check: sufficient evidence
# ---------------------------------------------------------------------------


def test_sufficient_evidence_no_issues() -> None:
    hits = [_hit(citation_role="primary_legal_basis", can_cite=True)]
    facts = ReviewFacts(cross_border_transfer=True)
    chunks_by_id = {h.chunk_id: _make_chunk(chunk_id=h.chunk_id) for h in hits}

    check = run_self_check(hits, facts, chunks_by_id)

    assert check.status == "sufficient"
    assert check.issues == []
    assert check.triggered_reasons == []
    assert check.second_retrieval_triggered is False


# ---------------------------------------------------------------------------
# Self-check: no primary legal basis
# ---------------------------------------------------------------------------


def test_no_primary_legal_basis_triggers_second_retrieval() -> None:
    hits = [_hit(citation_role="interpretation_auxiliary", can_cite=False)]
    facts = ReviewFacts(cross_border_transfer=True)
    chunks_by_id = {h.chunk_id: _make_chunk(chunk_id=h.chunk_id) for h in hits}

    check = run_self_check(hits, facts, chunks_by_id)

    assert check.status == "needs_second_retrieval"
    assert "no_primary_legal_basis" in check.triggered_reasons
    assert "only_auxiliary_evidence" in check.triggered_reasons
    assert check.second_retrieval_plan is not None


# ---------------------------------------------------------------------------
# Self-check: region mismatch
# ---------------------------------------------------------------------------


def test_region_mismatch_triggers_second_retrieval() -> None:
    hits = [_hit(chunk_id="c1", citation_role="primary_legal_basis")]
    facts = ReviewFacts(cross_border_transfer=True, region="上海")
    chunks_by_id = {
        "c1": _make_chunk(chunk_id="c1", applicable_region="CN-BJ"),
    }

    check = run_self_check(hits, facts, chunks_by_id)

    assert "region_mismatch" in check.triggered_reasons
    assert check.status == "needs_second_retrieval"


def test_region_match_does_not_trigger() -> None:
    hits = [_hit(chunk_id="c1", citation_role="primary_legal_basis")]
    facts = ReviewFacts(cross_border_transfer=True, region="上海")
    chunks_by_id = {
        "c1": _make_chunk(chunk_id="c1", applicable_region="CN-SH"),
    }

    check = run_self_check(hits, facts, chunks_by_id)

    assert "region_mismatch" not in check.triggered_reasons
    assert check.status == "sufficient"


# ---------------------------------------------------------------------------
# Self-check: industry mismatch
# ---------------------------------------------------------------------------


def test_industry_mismatch_triggers_second_retrieval() -> None:
    hits = [_hit(chunk_id="c1", citation_role="primary_legal_basis")]
    facts = ReviewFacts(cross_border_transfer=True, industry="汽车")
    chunks_by_id = {
        "c1": _make_chunk(chunk_id="c1").model_copy(
            update={"applicable_subjects": ["金融机构"], "topic_tags": ["金融"]}
        ),
    }

    check = run_self_check(hits, facts, chunks_by_id)

    assert "industry_mismatch" in check.triggered_reasons
    assert check.status == "needs_second_retrieval"


def test_industry_match_does_not_trigger() -> None:
    hits = [_hit(chunk_id="c1", citation_role="primary_legal_basis")]
    facts = ReviewFacts(cross_border_transfer=True, industry="汽车")
    chunks_by_id = {
        "c1": _make_chunk(chunk_id="c1").model_copy(
            update={"applicable_subjects": ["汽车数据处理者"], "topic_tags": ["汽车行业"]}
        ),
    }

    check = run_self_check(hits, facts, chunks_by_id)

    assert "industry_mismatch" not in check.triggered_reasons


# ---------------------------------------------------------------------------
# Self-check: only auxiliary evidence
# ---------------------------------------------------------------------------


def test_only_auxiliary_evidence_triggers() -> None:
    hits = [
        _hit(chunk_id="c1", citation_role="implementation_reference", can_cite=False),
        _hit(chunk_id="c2", citation_role="interpretation_auxiliary", can_cite=False),
    ]
    facts = ReviewFacts(cross_border_transfer=True)
    chunks_by_id = {h.chunk_id: _make_chunk(chunk_id=h.chunk_id) for h in hits}

    check = run_self_check(hits, facts, chunks_by_id)

    assert "only_auxiliary_evidence" in check.triggered_reasons
    assert "no_primary_legal_basis" in check.triggered_reasons


# ---------------------------------------------------------------------------
# Self-check: cross-border mismatch
# ---------------------------------------------------------------------------


def test_cross_border_mismatch_triggers() -> None:
    hits = [_hit(text="关于个人信息保护的一般规定。", title="个人信息保护法")]
    facts = ReviewFacts(cross_border_transfer=True)

    check = run_self_check(hits, facts, {})

    assert "cross_border_mismatch" in check.triggered_reasons


# ---------------------------------------------------------------------------
# Self-check: critical facts missing
# ---------------------------------------------------------------------------


def test_critical_facts_missing_alone_is_sufficient_with_warning() -> None:
    """When only critical facts are missing but evidence is good, status is
    sufficient with a warning. Missing facts is an input quality issue, not
    an evidence sufficiency issue — should NOT force abstention."""

    hits = [_hit(citation_role="primary_legal_basis")]
    facts = ReviewFacts(
        cross_border_transfer=True,
        missing_information=["legal_basis_or_consent"],
    )
    chunks_by_id = {h.chunk_id: _make_chunk(chunk_id=h.chunk_id) for h in hits}

    check = run_self_check(hits, facts, chunks_by_id)

    assert "critical_facts_missing" in check.triggered_reasons
    # Only critical facts missing + good evidence -> sufficient (with warning)
    assert check.status == "sufficient"
    assert check.second_retrieval_plan is None
    # Issue is still recorded as a warning
    assert len(check.issues) == 1
    assert check.issues[0].issue_type == "critical_facts_missing"


def test_critical_facts_with_other_issues_still_triggers_retrieval() -> None:
    """When critical facts missing AND other fixable issues, second retrieval runs."""

    hits = [_hit(citation_role="interpretation_auxiliary", can_cite=False)]
    facts = ReviewFacts(
        cross_border_transfer=True,
        missing_information=["legal_basis_or_consent"],
    )
    chunks_by_id = {h.chunk_id: _make_chunk(chunk_id=h.chunk_id) for h in hits}

    check = run_self_check(hits, facts, chunks_by_id)

    # Has fixable issues (no_primary_legal_basis) + critical_facts_missing
    assert check.status == "needs_second_retrieval"


# ---------------------------------------------------------------------------
# Second retrieval plan
# ---------------------------------------------------------------------------


def test_second_retrieval_plan_includes_expansions() -> None:
    issues = [
        EvidenceIssue(
            issue_type="no_primary_legal_basis",
            description="no primary legal basis",
        )
    ]
    facts = ReviewFacts(cross_border_transfer=True, region="上海", industry="汽车")

    plan = build_second_retrieval_plan(issues, facts, ["no_primary_legal_basis"])

    assert len(plan.expanded_queries) > 0
    assert plan.increased_top_k > 10
    assert plan.stronger_boost is True
    # Should include legal term expansions
    query_texts = [q.text for q in plan.expanded_queries]
    assert any("数据出境" in t or "安全评估" in t for t in query_texts)


def test_second_retrieval_plan_includes_fact_keywords() -> None:
    issues = [
        EvidenceIssue(
            issue_type="region_mismatch",
            description="region mismatch",
        )
    ]
    facts = ReviewFacts(
        cross_border_transfer=True,
        region="上海",
        data_types=["手机号"],
        overseas_recipient="新加坡",
    )

    plan = build_second_retrieval_plan(issues, facts, ["region_mismatch"])

    # Should include fact keyword query
    material_queries = [q for q in plan.expanded_queries if q.query_type == "material_fact"]
    assert len(material_queries) >= 1
    fact_text = material_queries[0].text
    assert "手机号" in fact_text
    assert "新加坡" in fact_text


# ---------------------------------------------------------------------------
# Post-second-retrieval evaluation
# ---------------------------------------------------------------------------


def test_evaluate_after_second_retrieval_sufficient() -> None:
    """After second retrieval, if evidence is now sufficient, status is sufficient."""

    hits = [_hit(citation_role="primary_legal_basis", can_cite=True)]
    facts = ReviewFacts(cross_border_transfer=True)
    chunks_by_id = {h.chunk_id: _make_chunk(chunk_id=h.chunk_id) for h in hits}

    check = evaluate_after_second_retrieval(hits, facts, chunks_by_id, [])

    assert check.status == "sufficient"
    assert check.second_retrieval_triggered is True


def test_evaluate_after_second_retrieval_still_insufficient() -> None:
    """After second retrieval, if still insufficient, no more retrieval."""

    hits = [_hit(citation_role="interpretation_auxiliary", can_cite=False)]
    facts = ReviewFacts(
        cross_border_transfer=True,
        missing_information=["legal_basis_or_consent"],
    )
    chunks_by_id = {h.chunk_id: _make_chunk(chunk_id=h.chunk_id) for h in hits}

    check = evaluate_after_second_retrieval(hits, facts, chunks_by_id, [])

    assert check.status == "insufficient"
    assert check.second_retrieval_triggered is True
    # Critical: never triggers another retrieval
    assert check.second_retrieval_plan is None
