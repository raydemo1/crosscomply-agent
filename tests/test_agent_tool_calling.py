"""Native tool envelopes, continuation, execution limits and public boundaries."""

import json
from copy import deepcopy
from io import BytesIO

import pytest

from law_agent.config import LLMConfig
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.agent import (
    AgentModel,
    AgentState,
    answer_agent,
    read_material_page,
    review_context,
    run_agent,
)
from law_agent.review.qa_agent import QAState, execute_qa
from law_agent.review.qa_store import InMemoryQAStore
from law_agent.review.tool_calling import InvalidToolCall
from tests.test_qa import Tools
from tests.test_qa import api as qa_api_fixture
from tests.test_review_llm import FakeClient, tool_message
from tests.test_semantic_grounding import _draft

api = qa_api_fixture


class AnswerClient(FakeClient):
    def stream_text(self, messages, **kwargs):
        self.answer_messages = messages
        yield "当前资料不足，需结合具体情况判断。"


@pytest.mark.parametrize("effort", ["none", "low"])
def test_client_uses_auto_native_tools_and_preserves_reasoning(effort, monkeypatch):
    client = OpenAICompatibleClient(LLMConfig(api_key="test", base_url="https://example.test/v1",
        beta_base_url="https://example.test/beta", model="test", timeout_seconds=10,
        structured_output_mode="json_object", reasoning_effort=effort))
    messages = [ChatMessage(role="assistant", content=None, reasoning_content="earlier reasoning",
                           tool_calls=[{"id": "a", "type": "function", "function": {"name": "read", "arguments": "{}"}}]),
                ChatMessage(role="tool", content="actual text", tool_call_id="a")]
    requests = []
    result = tool_message(("b", "finish", {"summary": "完成"}))

    def post(payload, url):
        requests.append((payload, url))
        return {"choices": [{"finish_reason": "tool_calls", "message": result}]}

    monkeypatch.setattr(client, "_post_chat", post)
    assert client.chat_tools(messages, tools=[{"type": "function"}]) == result
    payload, url = requests[0]
    assert url == "https://example.test/v1"
    assert payload["tool_choice"] == "auto" and "response_format" not in payload
    assert payload["messages"][0]["reasoning_content"] == "earlier reasoning"
    assert payload["messages"][1]["tool_call_id"] == "a"
    assert payload["thinking"]["type"] == ("enabled" if effort == "low" else "disabled")


@pytest.mark.parametrize("effort", ["none", "low"])
def test_final_stream_preserves_native_dialogue_and_tools_at_http_boundary(effort, monkeypatch):
    requests = []
    outputs = [
        tool_message(("read", "read_material", {"summary": "读取材料"}), reasoning="first-native-reasoning"),
        tool_message(("finish", "finish", {"summary": "有限回答", "evidence_gaps": ["尚未取得法律原文"]}),
                     reasoning="last-native-reasoning"),
    ]

    def respond(request, **kwargs):
        payload = json.loads(request.data)
        requests.append(payload)
        if payload["tool_choice"] == "auto":
            message = outputs.pop(0)
            calls = [{"index": i, **call} for i, call in enumerate(message["tool_calls"])]
            return BytesIO(('data: ' + json.dumps({"choices": [{"delta": {
                "tool_calls": calls, "reasoning_content": message["reasoning_content"]},
                "finish_reason": "tool_calls"}]}) + '\n\ndata: [DONE]\n\n').encode())
        return BytesIO(('data: ' + json.dumps({"choices": [{"delta": {"reasoning_content": "stream-private-reasoning"}}]}) +
            '\n\ndata: ' + json.dumps({"choices": [{"delta": {"content": "资料不足，暂不能确定。"}, "finish_reason": "stop"}]}) +
            '\n\ndata: [DONE]\n\n').encode())

    monkeypatch.setattr("urllib.request.urlopen", respond)
    config = LLMConfig(api_key="test", base_url="https://example.test/v1", beta_base_url="https://example.test/beta",
                       model="test", timeout_seconds=10, structured_output_mode="json_object", reasoning_effort=effort)
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    store.add_material(cid, {"id": "v1", "filename": "facts.txt", "parsed_text": "原始业务前提，不替换为摘要。"})
    run = store.enqueue(cid, question="能否适用？", material_ids=["v1"], request_id="1")
    updates = []
    execute_qa(store.get(cid), run, checkpoint=lambda **kw: updates.append(kw), chunks_path="unused",
               client=OpenAICompatibleClient(config), tools=Tools())
    initial, investigation, final = requests
    assert final["messages"][:len(investigation["messages"])] == investigation["messages"]
    assert initial["messages"][0] == investigation["messages"][0] == final["messages"][0]
    assert initial["tools"] == investigation["tools"] == final["tools"]
    assert initial["tool_choice"] == "auto" and final["tool_choice"] == "none"
    assert final["thinking"] == initial["thinking"]
    assert [m["reasoning_content"] for m in final["messages"] if m["role"] == "assistant"] == [
        "first-native-reasoning", "last-native-reasoning"]
    assert sum(m["role"] == "user" for m in final["messages"]) == sum(
        m["role"] == "user" for m in investigation["messages"])
    assert [m["tool_call_id"] for m in final["messages"] if m["role"] == "tool"] == ["read", "finish"]
    read = next(m for m in final["messages"] if m.get("tool_call_id") == "read")
    assert "原始业务前提，不替换为摘要。" in json.loads(read["content"])["observation"]["text"]
    control = json.loads(final["messages"][-1]["content"])["observation"]
    assert control["ready_to_answer"] is True and control["citation_registry"] == []
    assert "answer_points" not in final["tools"][-1]["function"]["parameters"]["properties"]
    assert updates[-1]["answer"] == "资料不足，暂不能确定。"
    assert "reasoning" not in json.dumps(updates[-1]["result"])


def test_batch_executes_every_call_serially_and_returns_actual_results():
    client = AnswerClient(outputs=[
        tool_message(("a", "read_material", {"summary": "读取附件", "offset": 0}),
                     ("b", "read_material", {"summary": "继续读取", "offset": 4})),
        tool_message(("c", "finish", {"summary": "结束"})),
    ])
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    store.add_material(cid, {"id": "m", "filename": "facts.txt", "parsed_text": "实际材料原文"})
    run = store.enqueue(cid, question="概括附件", material_ids=["m"], request_id="1")
    updates = []
    execute_qa(store.get(cid), run, checkpoint=lambda **kw: updates.append(kw), chunks_path="unused",
               client=client, tools=Tools())
    assert updates[-1]["status"] == "completed" and len(client.calls) == 2
    results = [m for m in client.calls[1] if m.role == "tool"]
    assert [m.tool_call_id for m in results] == ["a", "b"]
    assert "实际材料原文" in json.loads(results[0].content)["observation"]["text"]
    assert json.loads(results[0].content)["observation"]["next_read_request"] is None
    assert not json.loads(results[0].content)["observation"]["truncated"]
    assert json.loads(results[1].content)["observation"]["text"] != json.loads(results[0].content)["observation"]["text"]
    assert any(m.reasoning_content == "private reasoning" for m in client.answer_messages)
    assert client.answer_messages[:len(client.calls[-1])] == client.calls[-1]


def test_material_pages_return_executable_continuation_and_explicit_end():
    material = "甲" * 12000 + "末页"
    first = read_material_page(material, 0)
    assert first["read_mode"] == "text" and first["truncated"]
    assert first["next_offset"] == 12000
    last = read_material_page(material, **first["next_read_request"])
    assert first["text"] + last["text"] == material
    assert not last["truncated"] and last["next_offset"] is None and last["next_read_request"] is None


@pytest.mark.parametrize("offset", [-1, 3])
def test_invalid_material_offsets_return_an_actionable_error(offset):
    with pytest.raises(ValueError, match="next_read_request"):
        read_material_page("材料", offset)


@pytest.mark.parametrize("material", ["", "短材料"])
def test_complete_material_has_no_continuation(material):
    result = read_material_page(material, 0)
    assert result["text"] == material and not result["truncated"]
    assert result["next_offset"] is None and result["next_read_request"] is None


def test_json_action_in_content_is_not_executed_as_a_tool():
    client = FakeClient(outputs=[{
        "role": "assistant",
        "content": json.dumps({"action": "read_material", "summary": "读取材料"}),
    }])
    state = AgentState(goal="审查")
    model = AgentModel(model_id="test", client=client)

    with pytest.raises(InvalidToolCall, match="已注册工具"):
        model(state, {})

    assert not state.steps and not state.tool_session.pending
    assert len(client.calls) == 1 and "tools" in client.kwargs[0]


@pytest.mark.parametrize("arguments", ["not json", {"summary": "检索", "queries": [], "draft": {}}])
def test_invalid_arguments_reach_tool_result_then_agent_can_recover(arguments):
    client = AnswerClient(outputs=[
        tool_message(("bad", "search_evidence", arguments)),
        tool_message(("good", "finish", {"summary": "有限答复"})),
    ])
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    run = store.enqueue(cid, question="问题", material_ids=[], request_id="1")
    updates = []
    execute_qa(store.get(cid), run, checkpoint=lambda **kw: updates.append(kw), chunks_path="unused", client=client, tools=Tools())
    error = next(m for m in client.calls[1] if m.role == "tool")
    assert error.tool_call_id == "bad" and json.loads(error.content)["observation"]["error"]
    assert updates[-1]["status"] == "completed"


def test_terminal_action_mixed_with_batch_is_not_executed_or_silently_dropped():
    class NoReads(Tools):
        def search(self, *args, **kwargs):
            pytest.fail("不能执行含终止动作的混合批次")

    client = AnswerClient(outputs=[
        tool_message(("a", "finish", {"summary": "结束"}),
                     ("b", "search_evidence", {"summary": "检索", "queries": []})),
        tool_message(("c", "finish", {"summary": "单独结束"})),
    ])
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    run = store.enqueue(cid, question="问题", material_ids=[], request_id="1")
    updates = []
    execute_qa(store.get(cid), run, checkpoint=lambda **kw: updates.append(kw), chunks_path="unused", client=client, tools=NoReads())
    errors = [m for m in client.calls[1] if m.role == "tool"]
    assert [m.tool_call_id for m in errors] == ["a", "b"]
    assert all("error" in json.loads(m.content)["observation"] for m in errors)
    assert updates[-1]["status"] == "completed"


def test_batch_respects_turn_budget_and_closes_unexecuted_calls(monkeypatch):
    monkeypatch.setattr("law_agent.review.qa_agent.QAState", lambda **kw: QAState(**kw, max_turns=1))
    client = AnswerClient(outputs=[tool_message(
        ("a", "read_material", {"summary": "读第一段", "offset": 0}),
        ("b", "read_material", {"summary": "读下一段", "offset": 4}),
    )])
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    run = store.enqueue(cid, question="问题", material_ids=[], request_id="1")
    updates = []
    execute_qa(store.get(cid), run, checkpoint=lambda **kw: updates.append(kw), chunks_path="unused", client=client, tools=Tools())
    state = [u["state"] for u in updates if "state" in u][-1]
    assert len(state["steps"]) == 1 and not state["tool_session"]["pending"]
    results = [m for m in state["tool_session"]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in results] == ["a", "b"]
    assert "未执行" in results[-1]["content"]
    assert len(client.calls) == 1 and updates[-1]["status"] == "completed"


def test_waiting_review_restores_native_dialogue_and_receives_new_human_input():
    first_client = FakeClient(outputs=[tool_message(("ask", "request_input", {"summary": "补充事实", "question": "用途是什么？"}))])
    state = AgentState(goal="限定审查")
    kwargs = {"material": "原材料", "intake": {}, "search": lambda *_: [], "web_search": lambda *_: [],
              "finalize": lambda draft, state: {"review_result": draft.model_dump(mode="json")}, "checkpoint": lambda _: None}
    run_agent(state, decide=AgentModel(model_id="test", client=first_client), **kwargs)
    assert state.status == "waiting_input"
    state = AgentState.model_validate_json(state.model_dump_json())
    answer_agent(state, gate_id=state.gate_id, answer="仅用于员工管理")
    second = FakeClient(outputs=[tool_message(("finish", "finish", {"summary": "交付", "draft": _draft().model_dump(mode="json")}))])
    run_agent(state, decide=AgentModel(model_id="test", client=second), **kwargs)
    messages = second.calls[0]
    assert any(m.reasoning_content == "private reasoning" for m in messages)
    assert any(m.role == "tool" and m.tool_call_id == "ask" for m in messages)
    assert any("仅用于员工管理" in (m.content or "") for m in messages if m.role == "user")
    assert state.status == "completed" and not state.tool_session.pending


def test_rejected_review_resumes_original_dialogue_with_uncited_evidence_feedback():
    from law_agent.review.semantic_grounding import SemanticGroundingRejected, SemanticVerdict
    from tests.test_review_result_builder import _hit

    first = _draft().model_copy(update={"conclusion": "仍须履行甲义务。"})
    corrected = first.model_copy(update={"conclusion": "按条件乙适用例外。"})
    verdict = SemanticVerdict(status="unsupported", claim_checks=[], core_checks=[{
        "target": "conclusion", "status": "unsupported", "quote": first.conclusion,
        "reason": "遗漏本轮已取得的例外", "evidence_ids": ["exception"],
    }], conclusion_reason="正文与例外不一致")
    client = FakeClient(outputs=[
        tool_message(("rejected", "finish", {"summary": "提交", "draft": first.model_dump(mode="json")}),
                     reasoning="original-native-reasoning"),
        tool_message(("accepted", "finish", {"summary": "修订", "draft": corrected.model_dump(mode="json")}),
                     reasoning="continued-native-reasoning"),
    ])
    state = AgentState(goal="审查甲义务", evidence=[_hit(), _hit().model_copy(update={
        "chunk_id": "exception", "text": "条件乙成立时不适用甲义务。", "full_article_text": None,
    })])
    submissions = []

    def finalize(draft, current):
        submissions.append(draft)
        if len(submissions) == 1:
            raise SemanticGroundingRejected(verdict)
        return {"review_result": draft.model_dump(mode="json")}

    run_agent(state, material="冻结材料", intake={"id": "frozen", "facts": {"condition_b": True}},
              decide=AgentModel(model_id="test", client=client), search=lambda *_, **_kwargs: [],
              web_search=lambda *_: [], finalize=finalize, checkpoint=lambda _: None)
    messages = client.calls[1]
    assert messages[:len(client.calls[0])] == client.calls[0]
    original = next(m for m in messages if m.role == "assistant")
    assert original.reasoning_content == "original-native-reasoning"
    assert json.loads(original.tool_calls[0]["function"]["arguments"])["draft"] == first.model_dump(mode="json")
    feedback = json.loads(next(m for m in messages if m.tool_call_id == "rejected").content)["observation"]
    assert feedback["semantic_grounding"] == verdict.model_dump(mode="json")
    assert feedback["revision_targets"][0]["evidence_ids"] == ["exception"]
    assert "条件乙成立时不适用甲义务。" in messages[1].content
    assert state.status == "completed" and state.report_draft is None
    assert state.result["review_result"]["conclusion"] == corrected.conclusion
    assert [report.model_dump(mode="json") for report in submissions] == [
        report.model_dump(mode="json") for report in (first, corrected)
    ]
    assert len(client.calls) == 2


def test_evidence_updates_are_lossless_without_repeating_prior_hit_text():
    from tests.test_review_result_builder import _hit
    client = FakeClient(outputs=[tool_message(("a", "search_evidence", {"summary": "检索", "queries": [
        {"query_id": "q", "text": "规则", "query_type": "legal_issue"}]})),
        tool_message(("b", "read_material", {"summary": "阅读", "offset": 0}))])
    model = AgentModel(model_id="test", client=client)
    state = AgentState(goal="问题")
    model(state, {})
    hit = _hit().model_copy(update={"text": "唯一的法源正文"})
    state.evidence.append(hit)
    state.tool_session.complete({"chunk_ids": [hit.chunk_id]}, review_context(state, {}))
    saved = deepcopy(state.tool_session.messages)
    model(state, {})
    state.tool_session.complete({"text": "附件"}, review_context(state, {}))
    assert state.tool_session.messages[:len(saved)] == saved
    occurrences = [hit for m in state.tool_session.messages if m["role"] in {"tool", "user"}
                   for hit in json.loads(m["content"]).get("state", {}).get("evidence", [])]
    assert len(occurrences) == 1 and occurrences[0]["text"] == "唯一的法源正文"


def test_public_api_does_not_expose_private_protocol_or_mutate_persisted_data(api):
    client, app, *_ = api
    private = {"agent_state": {"goal": "公开目标", "tool_session": {"messages": [
        {"reasoning_content": "private reasoning"}]}}, "answer": "可见答复"}

    @app.get("/api/test-native-public-boundary")
    def expose():
        return private

    response = client.get("/api/test-native-public-boundary")
    assert response.status_code == 200
    assert "tool_session" not in response.text and "private reasoning" not in response.text
    assert response.json()["answer"] == "可见答复"
    assert "tool_session" in private["agent_state"]


def test_review_without_record_facts_keeps_original_material_law_and_native_dialogue():
    from law_agent.review.agent import AgentDecision
    from law_agent.review.schemas import ReviewFacts
    from tests.test_review_result_builder import _hit

    original = "材料A记录12万人；材料B记录120万人，统计期间、去重口径和业务范围相同。"
    material = original + "业务原文。" * 2500 + "材料末尾的原始限制条件。"
    law = _hit().model_copy(update={"text": "自当年1月1日起累计，按自然人去重。", "full_article_text": None})
    client = FakeClient(outputs=[
        tool_message(("search", "search_evidence", {"summary": "核对人数门槛", "queries": [
            {"query_id": "q", "text": "累计人数门槛", "query_type": "legal_issue"}]}), reasoning="search-reasoning"),
        tool_message(("read", "read_material", {"summary": "读取剩余原文", "offset": 12000}), reasoning="read-reasoning"),
        tool_message(("finish", "finish", {"summary": "保留冲突交付", "draft": _draft().model_dump(mode="json")}), reasoning="finish-reasoning"),
    ])
    model = AgentModel(model_id="test", client=client)
    facts = ReviewFacts(cross_border_transfer=False, annual_sensitive_count="0", as_of_date="2024-04-01")
    state = AgentState(goal="审查路径", facts=facts.model_copy(deep=True))
    result = run_agent(state, material=material, intake={"id": "frozen", "facts": facts.model_dump(mode="json")},
                       decide=model, search=lambda *_, **_kwargs: [law], web_search=lambda *_: [],
                       finalize=lambda draft, state: {"ok": True}, checkpoint=lambda _: None)
    assert result.status == "completed" and result.facts == facts
    assert [step.action for step in result.steps] == ["search_evidence", "read_material", "finish"]
    final = client.calls[-1]
    assert final[:len(client.calls[0])] == client.calls[0]
    assert final[:len(client.calls[1])] == client.calls[1]
    initial = json.loads(final[1].content)
    rest = json.loads(next(m.content for m in final if m.tool_call_id == "read"))
    assert initial["state"]["material_preview"]["text"] + rest["observation"]["text"] == material
    search = json.loads(next(m.content for m in final if m.tool_call_id == "search"))
    assert search["state"]["evidence"][0]["text"] == law.text
    assert [m.reasoning_content for m in final if m.role == "assistant"] == ["search-reasoning", "read-reasoning"]
    for kwargs in client.kwargs:
        tools = {t["function"]["name"]: t["function"] for t in kwargs["tools"]}
        assert "record_facts" not in tools
        assert all("facts" not in t["parameters"]["properties"] for t in tools.values())
        assert all("ReviewFacts" not in t["parameters"].get("$defs", {}) for t in tools.values())
    with pytest.raises(ValueError):
        AgentDecision(action="record_facts", summary="改写", facts={})
