import pytest

from law_agent.review.retrieval.selection import select_evidence
from law_agent.review.schemas import RetrievalHit


def hit(chunk_id, score=1.0, text="原文" * 100, source_id="source", **fields):
    return RetrievalHit(
        chunk_id=chunk_id, doc_id=source_id, source_id=source_id, title="来源",
        text=text, score=score, rank=0, retriever="hybrid",
        citation_role="primary_legal_basis", can_cite_clause=True,
        source_url="https://example.gov.cn/rule",
    ).model_copy(update=fields)


def test_best_query_match_wins_without_a_repeat_hit_bonus():
    repeated = hit("general", score=0.2, matched_query_ids=["q1"])
    specific = hit("specific", score=0.8, matched_query_ids=["q2"])
    selected = select_evidence([
        repeated, repeated.model_copy(update={"matched_query_ids": ["q2"]}),
        specific.model_copy(update={"score": 0.4}), specific,
    ], max_chunks=1)
    assert selected == [specific]
    all_hits = select_evidence([repeated, repeated.model_copy(update={"matched_query_ids": ["q2"]})], max_chunks=2)
    assert all_hits[0].matched_query_ids == ["q1", "q2"]
    assert all_hits[0].score == 0.2


def test_complete_article_duplicates_do_not_use_multiple_positions():
    article = "第一条 完整条件和例外。" * 30
    first = hit("first", score=0.9, article_no="第一条", full_article_text=article)
    duplicate = hit("duplicate", score=0.8, article_no="第一条", full_article_text=article)
    other = hit("other", score=0.7, article_no="第二条")
    selected = select_evidence([duplicate, first, other], max_chunks=2)
    assert [h.chunk_id for h in selected] == ["first", "other"]
    assert selected[0].full_article_text == article


def test_complete_long_article_is_kept_without_character_truncation():
    full = "完整长条文" * 1000
    selected = select_evidence([hit("long", score=10, article_no="第一条", full_article_text=full), hit("short")], max_chunks=1)
    assert selected[0].chunk_id == "long"
    assert selected[0].full_article_text == full


def test_selection_has_no_source_quota_and_preserves_qualification():
    reference = hit("reference", score=100, source_id="outside", can_cite_clause=False,
                    citation_role="implementation_reference")
    selected = select_evidence([hit("main"), reference], max_chunks=1)
    assert selected == [reference]
    assert selected[0].can_cite_clause is False


@pytest.mark.parametrize("limit", [1, 3, 10])
def test_limit_counts_all_queries_and_sources_together(limit):
    candidates = [hit(str(i), score=i, source_id=str(i), matched_query_ids=[f"q{i % 4}"]) for i in range(20)]
    assert len(select_evidence(candidates, max_chunks=limit)) == limit


@pytest.mark.parametrize("limit", [0, -1, 1.5, True])
def test_invalid_limit_is_rejected(limit):
    with pytest.raises(ValueError):
        select_evidence([], max_chunks=limit)


def test_chunks_of_one_reference_section_share_one_position():
    rows = [hit(f"row{i}", score=1 - i / 10, can_cite_clause=False, citation_role="implementation_reference",
                heading_path=["负面清单", "零售"]) for i in range(3)]
    other = hit("other", score=0.1, article_no="第五条")
    assert [h.chunk_id for h in select_evidence([*rows, other], max_chunks=2)] == ["row0", "other"]


def test_each_query_keeps_its_best_locations_against_a_broad_query():
    broad = [hit(f"broad{i}", score=0.9 - i / 100, rank=i, article_no=f"第{i}条", matched_query_ids=["q1"])
             for i in range(5)]
    narrow = [hit(f"narrow{i}", score=0.3 - i / 100, rank=i, article_no=f"第9{i}条", matched_query_ids=["q2"])
              for i in range(3)]
    selected = [h.chunk_id for h in select_evidence([*broad, *narrow], max_chunks=5)]
    assert selected == ["broad0", "narrow0", "broad1", "narrow1", "broad2"]


def test_selection_respects_post_spread_rank_and_keeps_deferred_hits_behind_listed_hits():
    # A deferred FAQ keeps its high raw score (0.98) but was moved to rank 8 (>= listed_depth).
    # Promoted formal clauses at rank 0..2 with lower scores must take the body slots first.
    promoted = [
        hit(f"law{i}", score=0.80 - i / 100, rank=i, article_no=f"第{i + 1}条", matched_query_ids=["q1"])
        for i in range(3)
    ]
    deferred_faq = hit(
        "faq_deferred", score=0.98, rank=8, source_id="faq", can_cite_clause=False,
        citation_role="interpretation_auxiliary", matched_query_ids=["q1"],
    )
    q2_law = hit("q2_law", score=0.75, rank=0, source_id="other", article_no="第十条", matched_query_ids=["q2"])
    selected = [h.chunk_id for h in select_evidence([*promoted, deferred_faq, q2_law], max_chunks=4, listed_depth=8)]
    assert "faq_deferred" not in selected
    assert set(selected) == {"law0", "law1", "law2", "q2_law"}

