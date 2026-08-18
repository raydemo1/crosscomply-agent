from __future__ import annotations

from law_agent.config import RerankConfig
from law_agent.review.retrieval.rerank import Reranker, RerankScore, rerank_hits
from law_agent.review.schemas import RetrievalHit, RetrievalQuery, ReviewFacts


class _FakeReranker(Reranker):
    def __init__(self, scores: list[RerankScore]) -> None:
        self.scores = scores
        self.seen_query = ""
        self.seen_documents: list[str] = []

    def rerank(self, *, query: str, documents, top_n: int) -> list[RerankScore]:
        self.seen_query = query
        self.seen_documents = list(documents)
        return self.scores


def _hit(chunk_id: str, rank: int, score: float = 0.1) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=chunk_id,
        doc_id=f"doc_{chunk_id}",
        source_id=f"src_{chunk_id}",
        title=f"title {chunk_id}",
        text=f"第{rank}条 数据出境安全评估 申报条件。",
        score=score,
        rank=rank,
        retriever="hybrid",
        citation_role="primary_legal_basis",
        can_cite_clause=True,
        source_url="u",
        matched_query_type="legal_issue",
    )


def _config(window: int = 3) -> RerankConfig:
    return RerankConfig(
        mode="embedding",
        base_url="https://example.test/v1",
        api_key="sk-test",
        model="BAAI/bge-reranker-v2-m3",
        timeout_seconds=30,
        window=window,
        blend_weight=0.4,
    )


def test_rerank_hits_reorders_window_and_keeps_missing_candidates() -> None:
    hits = [_hit("a", 0), _hit("b", 1), _hit("c", 2)]
    fake = _FakeReranker([RerankScore(index=2, score=0.9), RerankScore(index=0, score=0.2)])

    outcome = rerank_hits(
        hits,
        question="是否需要数据出境安全评估？",
        material_text="手机号同步给境外服务商。",
        facts=ReviewFacts(cross_border_transfer=True),
        queries=[
            RetrievalQuery(
                query_id="q1",
                query_type="legal_issue",
                text="数据出境安全评估 申报条件",
            )
        ],
        top_k=3,
        mode="embedding",
        config=_config(),
        reranker=fake,
    )

    assert [hit.chunk_id for hit in outcome.hits] == ["c", "a", "b"]
    assert [hit.rank for hit in outcome.hits] == [0, 1, 2]
    assert outcome.hits[0].score == 1.0
    assert outcome.info["mode"] == "embedding"
    assert outcome.info["model"] == "BAAI/bge-reranker-v2-m3"
    assert outcome.info["blend_weight"] == 0.4
    assert "业务事实" in fake.seen_query
    assert "证据角色" in fake.seen_documents[0]


def test_rerank_hits_off_preserves_original_order() -> None:
    hits = [_hit("a", 0), _hit("b", 1)]

    outcome = rerank_hits(
        hits,
        question="问题",
        material_text="材料",
        facts=ReviewFacts(),
        queries=[],
        top_k=2,
        mode="off",
    )

    assert outcome.hits == hits
    assert outcome.info["mode"] == "off"
