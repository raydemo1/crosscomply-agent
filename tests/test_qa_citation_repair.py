import json

import pytest

from law_agent.config import LLMConfig
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review import qa_citations
from law_agent.review.qa_agent import QAState
from law_agent.review.qa_citations import (
    CitationReferenceError,
    apply_citation_repairs,
    citation_marker,
    register_citation_marker,
    render_qa_citations,
)
from law_agent.review.qa_context import qa_state_context
from tests.test_qa import Tools
from tests.test_qa_native_answers import run_qa
from tests.test_review_llm import FakeClient, tool_message
from tests.test_review_result_builder import _hit


@pytest.mark.parametrize("make", [
    lambda key: f"[[CITE:{key.upper()}]]",
    lambda key: f"[[cite:{key[:4]} {key[4:]}]]",
    lambda key: f"[[cite:{key[:4]}\n{key[4:]}]]",
    lambda key: f"[cite:{key}]",
    lambda key: f"[[cite:{key}]",
    lambda key: f"[[cite:{key}",
])
def test_only_exact_registered_identity_is_automatically_normalized(make):
    marker = citation_marker("chunk:c1")
    assert len(marker[7:-2]) == 8
    sources = [{"citation_marker": marker, "ref": "依据7", "chunk_id": "c1"}]
    displayed, used = render_qa_citations("原句。" + make(marker[7:-2]), sources)
    assert displayed == "原句。[依据7]"
    assert [c["chunk_id"] for c in used] == ["c1"]


def test_short_marker_collisions_extend_only_new_identity_and_context_uses_registry(monkeypatch):
    def colliding(identity, *, length=8):
        return "[[cite:" + ("12345678" if length == 8 else "123456789abc") + "]]"

    monkeypatch.setattr(qa_citations, "citation_marker", colliding)
    markers = {}
    first = register_citation_marker("chunk:c1", markers)
    second = register_citation_marker("chunk:c2", markers)
    assert first != second
    assert register_citation_marker("chunk:c1", markers) == first
    state = QAState(goal="q", evidence=[_hit("c1"), _hit("c2")], citation_markers=markers)
    assert [h["citation_marker"] for h in qa_state_context(state.model_dump(mode="json"))["evidence"]] == [first, second]


def test_actual_failed_markers_are_collected_without_fuzzy_matching():
    sources = [{"citation_marker": "[[cite:14f04395f6d4b4da]]", "ref": "依据1"}]
    bad = ["[[cite:61368ea4d70fdb8af3104d5d]]", "[[cite:14f04395f6b4da]]"]
    with pytest.raises(CitationReferenceError) as error:
        render_qa_citations("甲。" + bad[0] + "乙。" + bad[1], sources)
    assert error.value.references == bad


def test_exact_acquired_chunk_identifier_is_repaired_but_ambiguous_suffix_is_not():
    chunk = "cac_cross_border_data_flow_rules_2024:61368ea4d70fdb8af3104d5d"
    marker = citation_marker("chunk:" + chunk)
    sources = [{"citation_marker": marker, "chunk_id": chunk, "ref": "依据6"}]
    for identity in (chunk, chunk.split(":")[-1]):
        assert render_qa_citations("判断。[[cite:" + identity + "]]", sources)[0] == "判断。[依据6]"
    other = "another_source:61368ea4d70fdb8af3104d5d"
    sources.append({"citation_marker": citation_marker("chunk:" + other), "chunk_id": other, "ref": "依据7"})
    with pytest.raises(CitationReferenceError):
        render_qa_citations("[[cite:61368ea4d70fdb8af3104d5d]]", sources)
    assert render_qa_citations("[[cite:" + chunk + "]]", sources)[0] == "[依据6]"


@pytest.mark.parametrize("patches", [
    [], [{"old": "[[cite:wrong]]", "new": "[[cite:foreign]]"}],
    [{"old": "[[cite:wrong]]", "new": ""}],
    [{"old": "原始正文", "new": "[[cite:valid]]"}],
    [{"old": "[[cite:wrong]]", "new": "[[cite:valid]]", "answer": "重写"}],
    [{"old": "[[cite:wrong]]", "new": "[[cite:valid]]"}] * 2,
])
def test_patch_cannot_delete_citations_rewrite_body_choose_foreign_source_or_repeat(patches):
    error = CitationReferenceError("未取得", ["[[cite:wrong]]"])
    with pytest.raises(CitationReferenceError):
        apply_citation_repairs("原始正文[[cite:wrong]]", error,
                               [{"citation_marker": "[[cite:valid]]"}], patches)


@pytest.mark.parametrize("mode", ["direct", "finish_body", "finish_stream"])
def test_one_strict_patch_preserves_original_answer_history_and_metadata(mode):
    bad, good = "[[cite:wrong]]", citation_marker("chunk:c1")
    text = "第一段原文。" + bad + "\n\n第二段原文。" + good
    search = tool_message(("search", "search_evidence", {"summary": "查依据", "queries": [
        {"query_id": "q", "text": "规则", "query_type": "legal_issue"}], "date_context": {"as_of_date": None}}), reasoning="调查原始思考")
    finish = tool_message(("finish", "finish", {"summary": "交付", "chunk_ids": ["c1"]}))
    if mode == "direct":
        answer = {"content": text, "reasoning_content": "原始回答思考"}
    elif mode == "finish_body":
        answer = {**finish, "content": text}
    else:
        answer = finish

    class Answer(FakeClient):
        def stream_text(self, messages, **kwargs):
            yield text

    llm = Answer(outputs=[search, answer, tool_message(("patch", "repair_citations", {
        "replacements": [{"old": bad, "new": good}]}))])
    updates = run_qa(llm)
    assert len(llm.calls) == 3
    tool = llm.kwargs[-1]["tools"][0]["function"]
    assert tool["strict"] is True
    props = tool["parameters"]["properties"]["replacements"]["items"]["properties"]
    assert props["old"]["enum"] == [bad] and props["new"]["enum"] == [good]
    assert any(m.reasoning_content == "调查原始思考" for m in llm.calls[-1])
    evidence = [hit for m in llm.calls[-1] if m.role == "tool"
                for hit in json.loads(m.content).get("state", {}).get("evidence", [])]
    assert any(hit["text"] == _hit("c1").text for hit in evidence)
    correction = json.loads(llm.calls[-1][-1].content)
    assert correction["answer"] == text
    assert any(u.get("answer") == "" and u.get("progress") == "正在校正引用" for u in updates)
    assert updates[-1]["status"] == "completed"
    assert updates[-1]["answer"] == "第一段原文。[依据1]\n\n第二段原文。[依据1]"
    assert updates[-1]["result"]["conditions"] == []
    assert [c["chunk_id"] for c in updates[-1]["result"]["citations"]] == ["c1"]
    state = [u["state"] for u in updates if "state" in u][-1]
    assert state["tool_session"]["messages"][-1]["role"] == "tool"
    assert not state["tool_session"]["pending"]
    assert [s["action"] for s in state["steps"]][-2:] == ["citation_error", "citation_repair"]


@pytest.mark.parametrize("mode", ["bad_patch", "no_patch", "budget_exhausted"])
def test_failed_or_unavailable_patch_still_blocks_and_never_loops(monkeypatch, mode):
    if mode == "budget_exhausted":
        monkeypatch.setattr("law_agent.review.qa_agent.QAState", lambda **kw: QAState(**kw, max_turns=2))
    llm = FakeClient(outputs=[tool_message(("search", "search_evidence", {"summary": "查依据", "queries": [
        {"query_id": "q", "text": "规则", "query_type": "legal_issue"}], "date_context": {"as_of_date": None}})),
        {"content": "原始正文[[cite:wrong]]"},
        tool_message(("patch", "repair_citations", {"replacements": [] if mode == "no_patch" else [
            {"old": "[[cite:wrong]]", "new": "[[cite:unacquired]]"}]})),
    ])
    with pytest.raises(CitationReferenceError):
        run_qa(llm, Tools())
    assert len(llm.calls) == (2 if mode == "budget_exhausted" else 3)


@pytest.mark.parametrize("effort", ["none", "low"])
def test_strict_native_call_uses_beta_with_auto_choice_and_same_thinking(effort, monkeypatch):
    client = OpenAICompatibleClient(LLMConfig(api_key="test", base_url="https://example.test/v1",
        beta_base_url="https://example.test/beta", model="test", timeout_seconds=10,
        structured_output_mode="json_object", reasoning_effort=effort))
    requests = []
    def post(payload, url):
        requests.append((payload, url))
        return {"choices": [{"finish_reason": "tool_calls", "message": tool_message(("a", "repair_citations", {}))}]}
    monkeypatch.setattr(client, "_post_chat", post)
    client.chat_tools([ChatMessage(role="user", content="校正引用")], tools=[{
        "type": "function", "function": {"name": "repair_citations", "strict": True}}])
    payload, url = requests[0]
    assert url == "https://example.test/beta" and payload["tool_choice"] == "auto"
    assert payload["thinking"]["type"] == ("disabled" if effort == "none" else "enabled")
    with pytest.raises(ValueError, match="所有function"):
        client.chat_tools([], tools=[{"function": {"strict": True}}, {"function": {}}])
