import json
from datetime import date

import pytest

from law_agent.review.qa_agent import QADateContext, execute_qa
from law_agent.review.qa_context import conversation_context
from law_agent.review.qa_store import InMemoryQAStore
from law_agent.review.web_research import WebSearchBatch
from tests.test_qa import StreamingClient, Tools
from tests.test_review_llm import tool_message


def query():
    return {"query_id": "q", "query_type": "legal_issue", "text": "适用规则"}


class RecordingTools(Tools):
    def __init__(self):
        self.dates = []

    def search(self, queries, facts, **kwargs):
        self.dates.append(("search", facts.as_of_date))
        return super().search(queries, facts, **kwargs)

    def read_evidence(self, source_id, article_no, chunk_id, facts, offset, **kwargs):
        self.dates.append(("read", facts.as_of_date))
        return super().read_evidence(source_id, article_no, chunk_id, facts, offset, **kwargs)

    def search_web(self, queries, facts, max_results):
        self.dates.append(("web", facts.as_of_date))
        return WebSearchBatch(findings=[])


def run(outputs, question="应该如何判断？", material="审查时点2026-10-08。统计期间2026年1月1日至10月8日。"):
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    store.add_material(cid, {"id": "selected", "filename": "材料.txt", "parsed_text": material})
    store.add_material(cid, {"id": "unselected", "filename": "其他.txt", "parsed_text": "判断日期2025-01-01"})
    request = store.enqueue(cid, question=question, material_ids=["selected"], request_id="1")
    client, tools, updates = StreamingClient(outputs=outputs), RecordingTools(), []
    execute_qa(store.get(cid), request, checkpoint=lambda **change: updates.append(change),
               chunks_path="unused", client=client, tools=tools)
    return client, tools, updates


def finish():
    return tool_message(("finish", "finish", {"summary": "回答", "chunk_ids": ["c1"]}))


def test_whitespace_is_not_a_date_source_quote():
    with pytest.raises(ValueError, match="quote"):
        QADateContext(as_of_date=date(2026, 10, 8), quote="   ")


def test_unconfirmed_date_is_tool_feedback_then_model_date_reaches_all_tools():
    client, tools, updates = run([
        tool_message(("missing", "search_evidence", {"summary": "检索", "queries": [query()]})),
        tool_message(("confirmed", "search_evidence", {"summary": "按材料时点检索", "queries": [query()],
            "date_context": {"as_of_date": "2026-10-08", "quote": "审查时点2026-10-08"}})),
        tool_message(("read", "read_evidence", {"summary": "精读", "read_requests": [{"source_id": "s1", "chunk_id": "c1"}]})),
        tool_message(("web", "search_web", {"summary": "补查", "queries": [query()]})),
        finish(),
    ])
    assert tools.dates == [(kind, date(2026, 10, 8)) for kind in ["search", "read", "web"]]
    state = next(u["state"] for u in reversed(updates) if "state" in u)
    assert state["date_context"] == {"as_of_date": "2026-10-08", "quote": "审查时点2026-10-08"}
    assert "quote" not in state["facts"]
    first = next(json.loads(m.content) for m in client.answer_messages if m.role == "tool")
    assert "as_of_date" in first["observation"]["error"]


def test_date_quote_must_come_from_selected_user_inputs():
    _, tools, updates = run([
        tool_message(("wrong", "search_evidence", {"summary": "检索", "queries": [query()],
            "date_context": {"as_of_date": "2025-01-01", "quote": "判断日期2025-01-01"}})),
        tool_message(("correct", "search_evidence", {"summary": "修正", "queries": [query()],
            "date_context": {"as_of_date": "2026-10-08", "quote": "审查时点2026-10-08"}})),
        finish(),
    ])
    assert tools.dates == [("search", date(2026, 10, 8))]
    assert updates[-1]["status"] == "completed"


def test_explicit_model_date_without_a_quote_is_rejected_before_research():
    _, tools, _ = run([
        tool_message(("unsupported", "search_evidence", {"summary": "检索", "queries": [query()],
            "date_context": {"as_of_date": "2026-10-28"}})),
        tool_message(("correct", "search_evidence", {"summary": "按材料检索", "queries": [query()],
            "date_context": {"as_of_date": "2026-10-08", "quote": "审查时点2026-10-08"}})),
        finish(),
    ])
    assert tools.dates == [("search", date(2026, 10, 8))]


def test_web_can_confirm_timepoint_before_local_search():
    _, tools, updates = run([
        tool_message(("research", "search_web", {"summary": "调查", "queries": [query()],
            "date_context": {"as_of_date": "2026-10-08", "quote": "审查时点2026-10-08"}})),
        tool_message(("finish", "finish", {"summary": "回答"})),
    ])
    assert tools.dates == [("web", date(2026, 10, 8))]
    assert updates[-1]["status"] == "completed"


def test_read_can_select_a_comparison_date_after_source_discovery():
    _, tools, _ = run([
        tool_message(("search", "search_evidence", {"summary": "找到来源", "queries": [query()],
            "date_context": {"as_of_date": None}})),
        tool_message(("read", "read_evidence", {"summary": "核对历史版本", "read_requests": [{"source_id": "s1", "chunk_id": "c1"}],
            "date_context": {"as_of_date": "2026-10-08", "quote": "审查时点2026-10-08"}})),
        finish(),
    ])
    assert tools.dates[-1] == ("read", date(2026, 10, 8))


def test_confirmed_scope_is_available_to_followup_without_reading_model_answer_as_evidence():
    material = {"id": "m", "filename": "说明.txt", "parsed_text": "审查时点2026-10-08"}
    previous = {"id": "first", "question": "需要什么机制？", "status": "completed", "material_ids": ["m"],
                "answer": "旧模型答复", "result": {}, "state": {
                    "date_context": {"as_of_date": "2026-10-08", "quote": "审查时点2026-10-08"},
                    "facts": {"as_of_date": "2026-10-08"},
                }}
    current = {"id": "next", "question": "继续", "material_ids": ["m"]}
    conversation = {"materials": [material], "runs": [previous, current]}

    history, _ = conversation_context(conversation, current)
    assert history[0]["previous_date_context"] == {
        "as_of_date": "2026-10-08", "quote": "审查时点2026-10-08",
    }
    current["material_ids"] = []
    history, _ = conversation_context(conversation, current)
    assert "previous_date_context" not in history[0]
