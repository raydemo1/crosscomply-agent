from datetime import date

import pytest

from law_agent.review.agent import AgentState, EvidenceReadRequest, _read_evidence_request
from law_agent.review.retrieval.locator import resolve_source_title
from law_agent.review.schemas import ReviewFacts
from tests.test_review_agent import _chunk, _tools_with


def test_explicit_title_reads_complete_article_without_search_and_continues_by_id():
    chunks = [_chunk(str(i), "第十三条", f"原文第{i}段", index=i) for i in range(7)]
    tools = _tools_with(chunks)
    state = AgentState(goal="核对条文")
    request = EvidenceReadRequest(source_title="《个人信息出境标准合同办法》", article_no="第13条")
    result = _read_evidence_request(state, request, read_evidence=tools.read_evidence, checkpoint=lambda _: None)
    assert result["resolved_source_id"] == "s1"
    assert result["truncated"] and result["next_read_request"]["source_id"] == "s1"
    assert result["next_read_request"]["source_title"] is None
    following = _read_evidence_request(state, EvidenceReadRequest(**result["next_read_request"]),
                                      read_evidence=tools.read_evidence, checkpoint=lambda _: None)
    assert not following["truncated"]
    assert [hit.text for hit in state.evidence] == [c.text for c in chunks]
    assert state.searches == 0 and state.reads == 2
    assert all(hit.can_cite_clause for hit in state.evidence)
    repeated = _read_evidence_request(state, EvidenceReadRequest(source_id="s1", article_no="第13条"),
                                     read_evidence=tools.read_evidence, checkpoint=lambda _: None)
    assert "已经读过" in repeated["error"] and state.reads == 2


@pytest.mark.parametrize("payload", [
    {}, {"source_title": "法规"}, {"source_title": "法规", "source_id": "s1", "article_no": "第一条"},
    {"source_title": "   ", "article_no": "第一条"},
    {"source_title": "法规", "article_no": "第一条", "chunk_id": "c1"},
])
def test_locator_requires_one_source_and_one_explicit_article(payload):
    with pytest.raises(ValueError):
        EvidenceReadRequest(**payload)


def test_title_resolution_preserves_version_date_and_does_not_guess_partial_titles():
    old = _chunk("old", "第一条", "旧版", index=0).model_copy(update={
        "source_id": "old", "title": "中华人民共和国某某法", "instrument_key": "same-law",
        "publish_date": "2020-01-01", "effective_date": "2020-02-01", "valid_to": "2025-01-01",
    })
    new = old.model_copy(update={"chunk_id": "new", "source_id": "new", "text": "新版",
                                 "publish_date": "2024-12-01", "effective_date": "2025-01-01", "valid_to": None})
    corpus = {c.chunk_id: c for c in [old, new]}
    assert resolve_source_title("《某某法》", corpus, as_of=date(2024, 1, 1)) == "old"
    assert resolve_source_title("中华人民 共和国某某法", corpus, as_of=date(2026, 1, 1)) == "new"
    for title in ["某某", "不存在的法律"]:
        with pytest.raises(ValueError, match="没有名称"):
            resolve_source_title(title, corpus, as_of=date(2026, 1, 1))
    with pytest.raises(ValueError, match="没有名称"):
        resolve_source_title("某某法", corpus, as_of=date(2019, 1, 1))


def test_ambiguous_current_sources_are_not_silently_selected():
    first = _chunk("one", "第一条", "原文一", index=0)
    second = first.model_copy(update={"chunk_id": "two", "source_id": "s2"})
    with pytest.raises(ValueError, match="对应多个来源"):
        resolve_source_title(first.title, {c.chunk_id: c for c in [first, second]}, as_of=date(2026, 1, 1))


def test_title_read_preserves_non_citable_qualification_and_reports_missing_article():
    chunk = _chunk("c1", "第一条", "解释材料", index=0).model_copy(update={
        "can_cite_clause": False, "citation_role": "interpretation_auxiliary",
    })
    tools = _tools_with([chunk])
    result = tools.read_evidence(None, "第一条", None, ReviewFacts(), source_title=chunk.title)
    assert not result.hits[0].can_cite_clause
    assert result.hits[0].citation_role == chunk.citation_role
    with pytest.raises(ValueError, match="没有条款"):
        tools.read_evidence(None, "第二条", None, ReviewFacts(), source_title=chunk.title)


def test_qa_can_start_with_title_read_and_show_grounded_evidence_to_model():
    import json

    from law_agent.review.qa_agent import execute_qa
    from law_agent.review.qa_store import InMemoryQAStore
    from tests.test_qa import StreamingClient
    from tests.test_review_llm import tool_message

    old = _chunk("old", "第一条", "旧版完整原文", index=0).model_copy(update={
        "source_id": "old", "instrument_key": "same-law", "publish_date": "2020-01-01",
        "effective_date": "2020-01-01", "valid_to": "2025-01-01",
    })
    new = old.model_copy(update={"chunk_id": "new", "source_id": "new", "text": "新版原文",
                                 "publish_date": "2025-01-01", "effective_date": "2025-01-01", "valid_to": None})
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    question = "请核对2024-01-01时该法第一条的原文"
    request = store.enqueue(cid, question=question, material_ids=[], request_id="1")
    client = StreamingClient(outputs=[
        tool_message(("read", "read_evidence", {"summary": "核对条文", "read_requests": [
            {"source_title": old.title, "article_no": "第一条"}],
            "date_context": {"as_of_date": "2024-01-01", "quote": question}})),
        tool_message(("finish", "finish", {"summary": "回答", "chunk_ids": ["old"]})),
    ])
    updates = []
    execute_qa(store.get(cid), request, checkpoint=lambda **change: updates.append(change),
               chunks_path="unused", client=client, tools=_tools_with([old, new]))
    state = next(u["state"] for u in reversed(updates) if "state" in u)
    assert state["searches"] == 0 and state["reads"] == 1
    assert state["evidence"][0]["text"] == old.text
    assert state["facts"]["as_of_date"] == "2024-01-01"
    model_context = json.dumps([m.content for m in client.answer_messages], ensure_ascii=False)
    assert old.text in model_context and new.text not in model_context
    assert updates[-1]["status"] == "completed"
