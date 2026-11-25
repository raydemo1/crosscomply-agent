from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace

import pytest

from law_agent.review.retrieval.adapters import ElasticsearchKeywordAdapter, PgVectorAdapter
from law_agent.review.retrieval.service_backends import make_pgvector_search_fn
from law_agent.review.schemas import RetrievalQuery, ReviewFacts
from tests.test_review_agent import _chunk, _tools_with


def test_both_backends_filter_before_limit_and_keep_valid_hit_after_fifty_future_hits():
    future = [_chunk(f"future-{i}", "第一条", "未来规则", index=0).model_copy(update={
        "source_id": f"future-{i}", "title": f"未来规则{i}", "effective_date": "2030-01-01",
    }) for i in range(50)]
    valid = _chunk("valid", "第二条", "历史时点有效的完整原文", index=0)
    rows = [{**chunk.model_dump(mode="json"), "score": 100 - index}
            for index, chunk in enumerate([*future, valid])]
    backend_calls = []

    class ES:
        def search(self, *, index, body):
            allowed = body["query"]["bool"]["filter"][0]["terms"]["chunk_id"]
            backend_calls.append(("keyword", body["size"], allowed))
            filtered = [row for row in rows if row["chunk_id"] in allowed][:body["size"]]
            return {"hits": {"hits": [{"_source": row, "_score": row["score"]} for row in filtered]}}

    class Cursor:
        description = tuple((key,) for key in rows[0])

        def execute(self, sql, parameters):
            assert sql.index("chunk_id = ANY(%s)") < sql.index("LIMIT")
            allowed, limit = parameters[1], parameters[-1]
            backend_calls.append(("vector", limit, allowed))
            self.rows = [tuple(row.values()) for row in rows if row["chunk_id"] in allowed][:limit]

        def fetchall(self):
            return self.rows

    class Connection:
        @contextmanager
        def transaction(self):
            yield

        @contextmanager
        def cursor(self):
            yield Cursor()

    tools = _tools_with([*future, valid])
    tools._top_k = 10
    tools._rerank_config = SimpleNamespace(window=50)
    tools._rerank_mode = "off"
    tools._question = "核对历史规则"
    tools._material_text = ""
    tools._adapters = SimpleNamespace(
        keyword=ElasticsearchKeywordAdapter(client=ES(), index_name="chunks"),
        vector=PgVectorAdapter(search_fn=make_pgvector_search_fn(Connection(), "chunks"),
                               embed_texts=lambda texts: [[1.0] for _ in texts]),
    )
    hits = tools.search([RetrievalQuery(query_id="q", query_type="legal_issue", text="规则")],
                        ReviewFacts(as_of_date=date(2024, 1, 1)))
    assert [hit.chunk_id for hit in hits] == ["valid"]
    assert backend_calls == [("keyword", 50, ["valid"]), ("vector", 50, ["valid"])]
    assert hits[0].text == valid.text and hits[0].can_cite_clause


@pytest.mark.parametrize("route", ["keyword", "vector"])
def test_empty_scope_does_not_call_backend_or_embedding_and_scope_count_is_validated(route):
    def forbidden(*args, **kwargs):
        pytest.fail("空范围不得执行后端或嵌入请求")

    adapter = (ElasticsearchKeywordAdapter(client=SimpleNamespace(search=forbidden), index_name="chunks")
               if route == "keyword" else PgVectorAdapter(search_fn=forbidden, embed_texts=forbidden))
    assert adapter.search_many([("规则", "legal_issue")], allowed_chunk_ids=[[]]) == [[]]
    with pytest.raises(ValueError, match="对应的候选范围"):
        adapter.search_many([("规则", "legal_issue")], allowed_chunk_ids=[])


def test_vector_queries_use_their_own_scope_and_share_cached_embedding():
    calls, embeddings = [], []

    def embed(texts):
        embeddings.append(texts)
        return [[1.0] for _ in texts]

    def search(vector, top_k, *, allowed_chunk_ids):
        calls.append(list(allowed_chunk_ids))
        return []

    adapter = PgVectorAdapter(search_fn=search, embed_texts=embed)
    adapter.search_many([("同一查询", "legal_issue"), ("同一查询", "material_fact")],
                        allowed_chunk_ids=[["first"], ["second"]])
    assert calls == [["first"], ["second"]]
    assert embeddings == [["同一查询"]]


def test_both_routes_contribute_to_bounded_candidates_before_body_selection():
    from law_agent.review.retrieval.neighbors import hit_from_chunk
    from tests.test_review_agent import _search_tools

    chunks = [_chunk(str(i), f"第{i + 1}条", f"原文{i}" * 100, index=i) for i in range(100)]
    keyword, vector = [[hit_from_chunk(c, i).model_copy(update={"score": 100 - i})
                        for i, c in enumerate(batch)] for batch in [chunks[:50], chunks[50:]]]
    tools = _search_tools(chunks, lambda *a, **k: [keyword], top_k=1)
    tools._adapters.vector = SimpleNamespace(search_many=lambda *a, **k: [vector])
    hits = tools.search([RetrievalQuery(query_id="q", query_type="legal_issue", text="规则")], ReviewFacts())
    assert len(tools._candidate_hits) == 50
    assert {"0", "50"} <= set(tools._candidate_hits)
    assert {"49", "99"}.isdisjoint(tools._candidate_hits)
    assert len(hits) == 1
    assert hits[0].chunk_id in tools._candidate_hits


def test_rerank_reads_clause_candidates_before_source_collapse_and_uses_current_query(monkeypatch):
    from law_agent.config import RerankConfig
    from law_agent.review.retrieval.neighbors import hit_from_chunk
    from law_agent.review.retrieval.rerank import RerankScore
    from tests.test_review_agent import _search_tools

    chunks = [_chunk(str(i), f"第{i + 1}条", f"候选原文{i}", index=i) for i in range(100)]
    batches = [[hit_from_chunk(c, i).model_copy(update={"score": 100 - i})
                for i, c in enumerate(part)] for part in [chunks[:50], chunks[50:]]]
    tools = _search_tools(chunks, lambda *a, **k: [batches[0]], top_k=1)
    tools._adapters.vector = SimpleNamespace(search_many=lambda *a, **k: [batches[1]])
    tools._rerank_mode = "cross_encoder"
    tools._rerank_config = RerankConfig(mode="embedding", base_url="unused", api_key=None,
                                        model="test", timeout_seconds=1, window=50, blend_weight=1.0)
    tools._question = "这是另一个层级的案件总问题"
    tools._material_text = "无关材料摘要不应冲淡当前检索意图"
    calls = []

    class Reranker:
        def rerank(self, *, query, documents, top_n):
            calls.append((query, documents, top_n))
            return [RerankScore(index=i, score=1.0 if "候选原文1\n" in doc + "\n" else 0.0)
                    for i, doc in enumerate(documents)]

    monkeypatch.setattr("law_agent.review.retrieval.rerank.build_reranker", lambda _: Reranker())
    hits = tools.search([RetrievalQuery(query_id="q", query_type="legal_issue", text="核对第二条")], ReviewFacts())
    assert len(calls) == 1
    assert calls[0][0] == "核对第二条"
    assert len(calls[0][1]) == calls[0][2] == 50
    assert any("候选原文0\n" in doc + "\n" for doc in calls[0][1])
    assert any("候选原文1\n" in doc + "\n" for doc in calls[0][1])
    assert hits[0].chunk_id == "1"
    assert hits[0].can_cite_clause


def test_vector_only_hits_take_article_metadata_from_the_corpus():
    from law_agent.review.retrieval.neighbors import hit_from_chunk
    from tests.test_review_agent import _search_tools

    chunks = [_chunk(str(i), f"第{i + 1}条", f"原文{i}" * 100, index=i) for i in range(3)]
    # The vector index returns rows without article numbers or headings.
    bare = [hit_from_chunk(c, i).model_copy(update={"article_no": None, "heading_path": [], "score": 3 - i})
            for i, c in enumerate(chunks)]
    tools = _search_tools(chunks, lambda *a, **k: [[]], top_k=1)
    tools._adapters.vector = SimpleNamespace(search_many=lambda *a, **k: [bare])
    tools.search([RetrievalQuery(query_id="q", query_type="legal_issue", text="规则")], ReviewFacts())
    assert [h.article_no for h in tools.last_query_candidates()[0]] == ["第1条", "第2条", "第3条"]
    locations = [loc for source in tools.search_results() for loc in source["locations"]]
    assert [(loc["article_no"], loc.get("acquired", False)) for loc in locations] == [
        ("第1条", True), ("第2条", False), ("第3条", False)]


def test_a_sectioned_document_keeps_two_listed_positions_while_law_articles_are_never_deferred():
    from law_agent.review.agent_tools import _spread_sources
    from law_agent.review.retrieval.neighbors import hit_from_chunk

    sections = [_chunk(f"list{i}", "", f"清单条目{i}", index=i).model_copy(update={
        "source_id": "ftz_list", "article_no": None, "can_cite_clause": False}) for i in range(4)]
    law = [_chunk(f"law{i}", f"第{i + 5}条", f"条文{i}", index=10 + i) for i in range(4)]
    ranked = [hit_from_chunk(c, i) for i, c in enumerate([*sections, *law, _chunk("tail", "第九条", "尾", index=20)])]
    spread = [h.chunk_id for h in _spread_sources(ranked)]
    # Only two list sections stay in front; every article of the law does, and the rest of the list follows.
    assert spread[:6] == ["list0", "list1", "law0", "law1", "law2", "law3"]
    assert spread[6:] == ["tail", "list2", "list3"]


def test_preview_starts_at_the_sentence_matching_the_query_and_names_its_section():
    from law_agent.review.agent_tools import preview_for
    from law_agent.review.retrieval.neighbors import hit_from_chunk

    answer = _chunk("qa", "", "答：此前已说明的背景情况。" * 6 + "第五条第一款第一项至第三项情形不计入累计数量。", index=0)
    hit = hit_from_chunk(answer, 0).model_copy(update={
        "article_no": None, "heading_path": ["政策问答", "问11：如何计算累计数量？"]})
    preview = preview_for(hit, "累计数量 不计入 豁免情形")
    assert preview.startswith("【问11：如何计算累计数量？】……第五条第一款第一项至第三项情形不计入累计数量")
    # No sentence matches: the opening is kept, with no ellipsis.
    assert preview_for(hit, "完全无关").startswith("【问11：如何计算累计数量？】答：此前已说明")
