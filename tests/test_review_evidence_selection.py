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
