
import json

import pytest

from law_agent.review.agent import AgentDecision
from law_agent.review.qa_agent import QADecision, execute_qa
from law_agent.review.qa_store import InMemoryQAStore
from law_agent.review.remediation import RemediationDecision
from law_agent.review.retrieval.neighbors import hit_from_chunk
from law_agent.review.schemas import ReviewFacts
from law_agent.review.tool_calling import CONTEXT_PROMPT, ToolCallingAgent, ToolSession
from tests.test_qa import StreamingClient, Tools
from tests.test_qa_retrieval import query
from tests.test_review_agent import _chunk, _search_tools
from tests.test_review_llm import tool_message
from tests.test_review_result_builder import _hit


def _seen_evidence(messages) -> dict[str, dict]:
    """Evidence originals the model has seen across user and tool messages, by chunk id."""
    seen: dict[str, dict] = {}
    for message in messages:
        if message["role"] not in {"user", "tool"}:
            continue
        try:
            payload = json.loads(message.get("content") or "")
        except ValueError:
            continue
        for hit in ((payload.get("state") or {}).get("evidence") or []) if isinstance(payload, dict) else []:
            seen.setdefault(hit["chunk_id"], hit)
    return seen


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
    local = _seen_evidence([{"role": m.role, "content": m.content} for m in client.answer_messages])
    assert set(local) == {"c0", "c1", "c2"}
    schema = next(t["function"]["parameters"] for t in client.kwargs[0]["tools"] if t["function"]["name"] == "search_evidence")
    assert schema["properties"]["max_chunks"]["maximum"] == 10
    assert "max_results" not in schema["properties"]


def test_two_searches_keep_all_unique_originals_in_native_messages():
    hits = [{"chunk_id": str(i), "source_id": "law", "text": f"原文第{i}段"} for i in range(15)]
    hits[2]["article_no"] = "第十三条"
    session = ToolSession()
    first = session.context_delta({"state": {"evidence": hits[:10], "evidence_sources": {"law": {"title": "法律"}}}})
    session.messages.append({"role": "user", "content": json.dumps(first, ensure_ascii=False)})
    original = session.messages[0]["content"]
    session.complete({"chunk_ids": [h["chunk_id"] for h in hits[5:]]},
                     {"state": {"evidence": hits, "evidence_sources": {"law": {"title": "法律"}}}})
    messages = ToolCallingAgent.messages(None, session, prompt="调查提示词")
    assert messages[0].content == "调查提示词" + CONTEXT_PROMPT
    assert messages[1].content == original
    assert len(json.loads(messages[-1].content)["state"]["evidence"]) == 5
    local = _seen_evidence([{"role": m.role, "content": m.content} for m in messages])
    assert len(local) == 15
    assert local["2"]["article_no"] == "第十三条"
    assert local["2"]["text"] == hits[2]["text"]
