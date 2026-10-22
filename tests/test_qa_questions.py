"""Business clarification terminates a QA turn and preserves attribution."""
import json

import pytest
from pydantic import ValidationError

from law_agent.review.qa_agent import QADecision, execute_qa
from law_agent.review.qa_context import conversation_context
from law_agent.review.qa_store import InMemoryQAStore
from tests.test_qa import StreamingClient, Tools
from tests.test_review_llm import tool_message

QUESTION = {"question": "实际累计出境人数是否已统计？", "selection": "single",
            "options": ["已统计，可以补充", "尚未统计"]}


def test_ask_user_stops_without_answer_call_and_survives_history():
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    store.enqueue(cid, question="日均处理50万人，是否需要申报？", material_ids=[], request_id="1")
    run = store.claim()["run"]
    client = StreamingClient(outputs=[tool_message(("ask", "ask_user", {
        "summary": "确认实际出境规模", "questions": [QUESTION]}))])
    execute_qa(store.get(cid), run, chunks_path="unused", tools=Tools(), client=client,
               checkpoint=lambda **changes: store.checkpoint(cid, run["id"], run["attempt"], **changes))
    saved = store.get(cid)["runs"][0]
    assert saved["status"] == "completed"
    assert saved["result"]["questions"] == [QUESTION]
    assert len(client.calls) == 1
    assert not hasattr(client, "answer_messages")
    assert not saved["state"]["tool_session"]["pending"]
    followup = store.enqueue(cid, question=QUESTION["question"] + "\n不清楚", material_ids=[], request_id="2")
    history, _ = conversation_context(store.get(cid), followup)
    assert QUESTION["question"] in history[0]["previous_model_answer"]
    assert history[0]["user_statement"] == "日均处理50万人，是否需要申报？"
    assert "不清楚" not in history[0]["user_statement"]


@pytest.mark.parametrize("questions", [[], [dict(QUESTION, options=["是", "是"])],
    [dict(QUESTION, options=[" ", "否"])], [dict(QUESTION, question=" ")],
    [dict(QUESTION, selection="all")], [QUESTION] * 4])
def test_invalid_question_packets_rejected(questions):
    with pytest.raises(ValidationError):
        QADecision(action="ask_user", summary="确认事实", questions=questions)






@pytest.mark.parametrize("reply", ["不清楚，尚未统计", "实际累计出境12万人，敏感个人信息0人",
                                  "更正：此前120万人有误，应为12万人"])
def test_followup_request_keeps_user_reply_and_original_goal_without_promoting_options(reply):
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    store.enqueue(cid, question="日均处理50万人，是否需要申报？", material_ids=[], request_id="1")
    first = store.claim()["run"]
    first_client = StreamingClient(outputs=[tool_message(("ask", "ask_user", {
        "summary": "确认人数", "questions": [QUESTION]}))])
    execute_qa(store.get(cid), first, chunks_path="unused", tools=Tools(), client=first_client,
        checkpoint=lambda **changes: store.checkpoint(cid, first["id"], first["attempt"], **changes))
    store.enqueue(cid, question=reply, material_ids=[], request_id="2")
    followup = store.claim()["run"]
    client = StreamingClient(outputs=[{"role": "assistant", "content": "根据用户补充继续回答。"}])
    execute_qa(store.get(cid), followup, chunks_path="unused", tools=Tools(), client=client,
        checkpoint=lambda **changes: store.checkpoint(cid, followup["id"], followup["attempt"], **changes))
    context = json.loads(client.calls[0][1].content)
    assert context["state"]["goal"] == reply
    assert context["history"][0]["user_statement"] == "日均处理50万人，是否需要申报？"
    assert QUESTION["question"] in context["history"][0]["previous_model_answer"]
    assert "已统计，可以补充" not in context["history"][0]["user_statement"]
    assert not client.calls[0][1].reasoning_content
    assert len(client.calls) == 1
    assert store.get(cid)["runs"][-1]["status"] == "completed"
