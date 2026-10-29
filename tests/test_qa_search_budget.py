import json

import pytest

from law_agent.review.agent import AgentDecision
from law_agent.review.qa_agent import QADecision, execute_qa
from law_agent.review.remediation import RemediationDecision
from law_agent.review.qa_store import InMemoryQAStore
from law_agent.review.retrieval.neighbors import hit_from_chunk
from law_agent.review.schemas import ReviewFacts
from scripts.eval_qa_retrieval_chain import visible_texts
from tests.test_qa import StreamingClient, Tools
from tests.test_qa_retrieval import query
from tests.test_review_agent import _chunk, _search_tools
from tests.test_review_llm import tool_message
from tests.test_review_result_builder import _hit


@pytest.mark.parametrize("decision_type", [QADecision, AgentDecision, RemediationDecision])
@pytest.mark.parametrize("limit", [0, 11, True, "2"])
def test_local_budget_rejects_invalid_counts(limit, decision_type):
    with pytest.raises(ValueError):
        decision_type(action="search_evidence", summary="核对条件", queries=[query()], max_chunks=limit)


def test_search_limit_applies_after_fusion_without_cutting_article_exceptions():
    main = _chunk("main", "第一条", "第一条 一般条件。" * 30, index=0, next_id="rest")
    rest = _chunk("rest", "第一条", "但是符合本条例外的除外。", index=1, prev="main")
    other = _chunk("other", "第二条", "第二条 其他条件。" * 30, index=2)
    third = _chunk("third", "第三条", "第三条 其他规则。" * 30, index=3)
    hits = [hit_from_chunk(c, i) for i, c in enumerate([main, other, third])]
    tools = _search_tools([main, rest, other, third], lambda *_a, **_kw: [hits, hits], top_k=10)
    queries = [query("一般条件", "q1"), query("其他条件", "q2")]
    unrestricted = tools.search(queries, ReviewFacts())
    limited = tools.search(queries, ReviewFacts(), max_chunks=1)
    assert len(unrestricted) > 1
    assert [h.chunk_id for h in limited] == [unrestricted[0].chunk_id]
    assert "但是符合本条例外的除外。" in limited[0].full_article_text
    assert tools.read_evidence("s1", "第二条", None, ReviewFacts()).hits[0].text == other.text


def test_qa_forwards_each_budget_and_keeps_previous_originals():
    calls = []

    class BudgetTools(Tools):
        def search(self, queries, facts, *, max_chunks, **kwargs):
            calls.append(max_chunks)
            start = 0 if len(calls) == 1 else 2
            return [_hit().model_copy(update={"chunk_id": f"c{i}", "text": f"原文{i}"})
                    for i in range(start, start + max_chunks)]

    client = StreamingClient(outputs=[
        tool_message(("first", "search_evidence", {"summary": "核对一般条件", "queries": [query().model_dump()],
                     "max_chunks": 2, "date_context": {"as_of_date": None}})),
        tool_message(("second", "search_evidence", {"summary": "核对尚缺的例外", "queries": [query("例外").model_dump()], "max_chunks": 1})),
        tool_message(("done", "finish", {"summary": "依据齐备", "chunk_ids": ["c0", "c1", "c2"]})),
    ])
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    run = store.enqueue(cid, question="条件与例外是什么？", material_ids=[], request_id="1")
    updates = []
    execute_qa(store.get(cid), run, checkpoint=lambda **kw: updates.append(kw), chunks_path="unused",
               client=client, tools=BudgetTools())
    assert updates[-1]["status"] == "completed"
    assert calls == [2, 1]
    local, _, _ = visible_texts({"messages": [{"role": m.role, "content": m.content} for m in client.answer_messages]})
    assert set(local) == {"c0", "c1", "c2"}
    schema = next(t["function"]["parameters"] for t in client.kwargs[0]["tools"] if t["function"]["name"] == "search_evidence")
    assert schema["properties"]["max_chunks"]["maximum"] == 10
    assert "max_results" not in schema["properties"]
