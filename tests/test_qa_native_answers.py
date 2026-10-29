"""Native text completion terminates QA without a second answer request."""

import json
from io import BytesIO
from itertools import count
from types import SimpleNamespace

import pytest

from law_agent.config import LLMConfig
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.agent import AgentModel, AgentState
from law_agent.review.qa_agent import execute_qa
from law_agent.review.qa_citations import citation_marker
from law_agent.review.qa_store import InMemoryQAStore
from law_agent.review.tool_calling import InvalidToolCall
from tests.test_qa import Tools
from tests.test_review_llm import FakeClient, tool_message
from tests.test_review_result_builder import _hit


def client(effort="low"):
    return OpenAICompatibleClient(LLMConfig(api_key="test", base_url="https://example.test/v1",
        beta_base_url="https://example.test/beta", model="test", timeout_seconds=10,
        structured_output_mode="json_object", reasoning_effort=effort))


def sse(*choices, done=True):
    lines = ["data: " + json.dumps({"choices": [choice]}, ensure_ascii=False) + "\n\n" for choice in choices]
    if done:
        lines.append("data: [DONE]\n\n")
    return BytesIO("".join(lines).encode("utf-8"))


def run_qa(llm, tools=None, **kwargs):
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    run = store.enqueue(cid, question="这个机制是否适用？", material_ids=[], request_id="1")
    updates = []
    execute_qa(store.get(cid), run, checkpoint=lambda **changes: updates.append(changes),
               client=llm, tools=tools or Tools(), chunks_path="unused", **kwargs)
    return updates


@pytest.mark.parametrize("effort", ["none", "low"])
@pytest.mark.parametrize("spaced_marker", [False, True])
def test_native_stream_answers_once_with_stable_refs_and_original_history(monkeypatch, effort, spaced_marker):
    ticks = count()
    monkeypatch.setattr("law_agent.review.qa_agent.time", SimpleNamespace(monotonic=lambda: next(ticks)))
    requests = []
    marker = citation_marker("chunk:c2")
    if spaced_marker:
        marker = marker.replace("[[", "[ [").replace("]]", "] ]")

    class TwoSources(Tools):
        def search(self, *args, **kwargs):
            return [_hit("c1"), _hit("c2")]

    def respond(request, **kwargs):
        payload = json.loads(request.data)
        requests.append(payload)
        assert payload["stream"] and payload["tool_choice"] == "auto"
        if len(requests) == 1:
            call = tool_message(("search", "search_evidence", {"summary": "查找依据", "queries": [
                {"query_id": "q", "text": "适用条件", "query_type": "legal_issue"}], "date_context": {"as_of_date": None}}))["tool_calls"][0]
            return sse({"delta": {"reasoning_content": "保留调查思考", "tool_calls": [{"index": 0, **call}]},
                        "finish_reason": "tool_calls"})
        assert len(requests) == 2, "直接答复后不得追加finish或再次生成"
        state = next(json.loads(m["content"])["state"] for m in payload["messages"]
                     if m["role"] == "tool" and "evidence" in json.loads(m["content"]).get("state", {}))
        assert [(h["chunk_id"], h["citation_marker"]) for h in state["evidence"]] == [
            ("c1", citation_marker("chunk:c1")), ("c2", citation_marker("chunk:c2"))]
        assert all("ref" not in h for h in state["evidence"])
        assert payload["messages"][2]["reasoning_content"] == "保留调查思考"
        return sse({"delta": {"reasoning_content": "最终思考不展示"}},
                   {"delta": {"content": "原始答复"}},
                   {"delta": {"content": "原文不改写。" + marker}, "finish_reason": "stop"})

    monkeypatch.setattr("urllib.request.urlopen", respond)
    updates = run_qa(client(effort), TwoSources())
    assert updates[-1]["status"] == "completed"
    assert updates[-1]["answer"] == "原始答复原文不改写。[依据2]"
    assert any(u.get("answer") == "原始答复" and "status" not in u for u in updates)
    assert [c["ref"] for c in updates[-1]["result"]["citations"]] == ["依据2"]
    state = [u["state"] for u in updates if "state" in u][-1]
    assert len(state["usage"]) == 2
    assert not any(s["action"] in {"finish", "tool_error"} for s in state["steps"])
    assert state["tool_session"]["messages"][-1]["content"] == "原始答复原文不改写。" + marker
    assert "最终思考" not in json.dumps(updates[-1]["result"], ensure_ascii=False)


@pytest.mark.parametrize("reference", ["依据99", "依据1 相关问答"])
def test_direct_answer_cannot_bypass_reference_integrity(monkeypatch, reference):
    requests = []

    def respond(request, **kwargs):
        requests.append(json.loads(request.data))
        return sse({"delta": {"content": f"回答。[{reference}]"}, "finish_reason": "stop"})

    monkeypatch.setattr("urllib.request.urlopen", respond)
    with pytest.raises(ValueError, match="引用编号"):
        run_qa(client())
    assert len(requests) == 1


def test_stream_interruption_preserves_partial_answer_and_does_not_regenerate(monkeypatch):
    ticks = count()
    monkeypatch.setattr("law_agent.review.qa_agent.time", SimpleNamespace(monotonic=lambda: next(ticks)))
    requests, updates = [], []

    def respond(request, **kwargs):
        requests.append(json.loads(request.data))
        return sse({"delta": {"content": "部分正文"}}, done=False)

    monkeypatch.setattr("urllib.request.urlopen", respond)
    from law_agent.review.llm import ReviewWorkflowFailed

    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    run = store.enqueue(cid, question="问题", material_ids=[], request_id="1")
    with pytest.raises(ReviewWorkflowFailed):
        execute_qa(store.get(cid), run, checkpoint=lambda **kw: updates.append(kw),
                   chunks_path="unused", client=client(), tools=Tools())
    assert len(requests) == 1
    assert any(u.get("answer") == "部分正文" for u in updates)
    state = [u["state"] for u in updates if "state" in u][-1]
    assert state["usage"][0]["status"] == "failed"
    assert not any(u.get("status") == "completed" for u in updates)


def test_streamed_tool_arguments_are_assembled_not_published(monkeypatch):
    def respond(request, **kwargs):
        return sse({"delta": {"reasoning_content": "私有", "tool_calls": [{"index": 0, "id": "call-a",
            "type": "function", "function": {"name": "read", "arguments": '{"off'}}]}},
            {"delta": {"tool_calls": [{"index": 1, "id": "call-b", "type": "function", "function": {
                "name": "search", "arguments": "{}"}}, {"index": 0, "function": {"arguments": 'set":0}'}}]},
             "finish_reason": "tool_calls"})

    monkeypatch.setattr("urllib.request.urlopen", respond)
    visible = []
    message = client().chat_tools([ChatMessage(role="user", content="调查")], tools=[], on_text=visible.append)
    assert visible == []
    assert message["reasoning_content"] == "私有"
    assert [c["id"] for c in message["tool_calls"]] == ["call-a", "call-b"]
    assert json.loads(message["tool_calls"][0]["function"]["arguments"]) == {"offset": 0}


def test_formal_review_still_requires_a_structured_tool_submission():
    llm = FakeClient(outputs=[{"role": "assistant", "content": "这是正文，不是审查报告提交"}])
    with pytest.raises(InvalidToolCall, match="已注册工具"):
        AgentModel(model_id="test", client=llm)(AgentState(goal="正式审查"), {})


def test_tool_preamble_is_cleared_before_final_answer(monkeypatch):
    ticks = count()
    monkeypatch.setattr("law_agent.review.qa_agent.time", SimpleNamespace(monotonic=lambda: next(ticks)))
    requests = []

    def respond(request, **kwargs):
        requests.append(json.loads(request.data))
        if len(requests) == 1:
            call = tool_message(("search", "search_evidence", {"summary": "检索", "queries": [
                {"query_id": "q", "text": "规则", "query_type": "legal_issue"}], "date_context": {"as_of_date": None}}))["tool_calls"][0]
            return sse({"delta": {"content": "先查询原文。", "tool_calls": [{"index": 0, **call}]},
                        "finish_reason": "tool_calls"})
        return sse({"delta": {"content": "这是最终回答。" + citation_marker("chunk:c1")}, "finish_reason": "stop"})

    monkeypatch.setattr("urllib.request.urlopen", respond)
    updates = run_qa(client())
    assert any(u.get("answer") == "先查询原文。" for u in updates)
    assert any(u.get("answer") == "" for u in updates)
    assert updates[-1]["answer"] == "这是最终回答。[依据1]"
    assert len(requests) == 2


def test_body_with_finish_keeps_material_metadata_without_second_generation(monkeypatch):
    requests = []

    def respond(request, **kwargs):
        payload = json.loads(request.data)
        requests.append(payload)
        assert payload["tool_choice"] == "auto", "已有正文时不得再次请求生成"
        if len(requests) == 1:
            message = tool_message(("search", "search_evidence", {"summary": "检索", "queries": [
                {"query_id": "q", "text": "规则", "query_type": "legal_issue"}], "date_context": {"as_of_date": None}}))
            return sse({"delta": {"tool_calls": [{"index": 0, **message["tool_calls"][0]}]},
                        "finish_reason": "tool_calls"})
        assert len(requests) == 2
        call = tool_message(("finish", "finish", {"summary": "交付", "chunk_ids": ["c1"],
            "material_citations": [{"material_version_id": "m1", "quote": "材料原句"}]}))["tool_calls"][0]
        return sse({"delta": {"content": "一次生成的完整答复。" + citation_marker("chunk:c1")}},
                   {"delta": {"tool_calls": [{"index": 0, **call}]}, "finish_reason": "tool_calls"})

    monkeypatch.setattr("urllib.request.urlopen", respond)
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    store.add_material(cid, {"id": "m1", "filename": "业务.txt", "parsed_text": "材料原句"})
    run = store.enqueue(cid, question="能否适用？", material_ids=["m1"], request_id="1")
    updates = []
    execute_qa(store.get(cid), run, checkpoint=lambda **kw: updates.append(kw), client=client(),
               chunks_path="unused", tools=Tools())
    assert len(requests) == 2
    result = updates[-1]["result"]
    assert result["answer"] == "一次生成的完整答复。[依据1]"
    assert result["conditions"] == []
    assert result["material_citations"] == [{"material_version_id": "m1", "filename": "业务.txt", "quote": "材料原句"}]
    state = [u["state"] for u in updates if "state" in u][-1]
    assert not state["tool_session"]["pending"]
    assert json.loads(state["tool_session"]["messages"][-1]["content"])["observation"]["completed"]


def test_finish_preselection_does_not_renumber_or_hide_other_acquired_refs():
    class TwoSources(Tools):
        def search(self, *args, **kwargs):
            return [_hit("c1"), _hit("c2")]

    class Answer(FakeClient):
        def stream_text(self, messages, **kwargs):
            yield "仅引用第二个已取得来源。" + citation_marker("chunk:c2")

    llm = Answer(outputs=[
        tool_message(("search", "search_evidence", {"summary": "调查", "queries": [
            {"query_id": "q", "text": "规则", "query_type": "legal_issue"}], "date_context": {"as_of_date": None}})),
        tool_message(("finish", "finish", {"summary": "登记", "chunk_ids": ["c1"]})),
    ])
    updates = run_qa(llm, TwoSources())
    assert updates[-1]["status"] == "completed"
    assert [c["ref"] for c in updates[-1]["result"]["citations"]] == ["依据2"]
