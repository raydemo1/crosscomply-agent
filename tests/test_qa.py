"""Private Q&A, bounded autonomous research and explicit review handoff."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from fastapi.testclient import TestClient

from law_agent.config import LLMConfig
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.api import create_app
from law_agent.review.case_store import InMemoryCaseStore
from law_agent.review.dates import business_today
from law_agent.review.enterprise_store import InMemoryEnterpriseStore
from law_agent.review.object_store import MaterialObjectStore
from law_agent.review.qa_agent import (
    PROMPT,
    QADecision,
    QAState,
    QAWorker,
    execute_qa,
    prepare_citations,
)
from law_agent.review.qa_citations import citation_marker
from law_agent.review.qa_store import InMemoryQAStore, QAConflict
from tests.test_enterprise_task_api import FakeMinio
from tests.test_review_llm import FakeClient, tool_message
from tests.test_review_result_builder import _hit


@pytest.fixture
def api(tmp_path):
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text("", encoding="utf-8")
    cases = InMemoryCaseStore(seed_password="pw")
    enterprise = InMemoryEnterpriseStore()
    app = create_app(chunks_path=chunks, case_store=cases, enterprise_store=enterprise,
                     material_object_store=MaterialObjectStore(client=FakeMinio(), bucket="qa"))
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "requester@crosscomply.local", "password": "pw"}).status_code == 200
        yield client, app, cases, enterprise


def enqueue(client, conversation_id, **changes):
    return client.post(f"/api/qa/conversations/{conversation_id}/messages", json={
        "question": "员工数据出境需要什么条件？", "request_id": "send-1", **changes,
    })


def completed(store, conversation_id):
    claimed = store.claim()["run"]
    store.checkpoint(conversation_id, claimed["id"], claimed["attempt"], status="completed", answer="需根据具体业务判断。",
                     result={"citations": [], "material_citations": [], "conditions": ["确认用途"]})
    return claimed


def test_owner_history_and_every_resource_are_private(api):
    client, _app, *_ = api
    conversation = client.post("/api/qa/conversations").json()
    path = f"/api/qa/conversations/{conversation['id']}"
    run = enqueue(client, conversation["id"]).json()
    assert client.get("/api/qa/conversations").json()["items"][0]["id"] == conversation["id"]
    client.post("/api/auth/logout")
    assert client.get(path).status_code == 401
    client.post("/api/auth/login", json={"username": "reviewer@crosscomply.local", "password": "pw"})
    assert client.get("/api/qa/conversations").json() == {"items": []}
    for suffix in ("", f"/runs/{run['id']}", f"/runs/{run['id']}/events", "/materials/not-mine/original"):
        assert client.get(path + suffix).status_code == 404
    assert enqueue(client, conversation["id"]).status_code == 404
    assert client.post(path + "/materials", files={"file": ("x.txt", b"abc", "text/plain")}).status_code == 404
    assert client.post(path + f"/runs/{run['id']}/retry").status_code == 404
    assert client.post(path + "/cases", json={"question": "q", "material_text": "m", "run_id": run["id"], "request_id": "x"}).status_code == 404


def test_message_dedup_concurrency_and_material_membership(api):
    client, app, cases, enterprise = api
    cid = client.post("/api/qa/conversations").json()["id"]
    baseline = deepcopy((cases.cases, enterprise.tasks))
    assert enqueue(client, cid, material_ids=["foreign"]).status_code == 409
    first = enqueue(client, cid).json()
    assert enqueue(client, cid).json()["id"] == first["id"]
    assert enqueue(client, cid, question="不同内容").status_code == 409
    assert enqueue(client, cid, request_id="send-2").status_code == 409
    assert enqueue(client, cid, question="  ").status_code == 422
    assert len(app.state.qa_store.get(cid)["runs"]) == 1
    assert (cases.cases, enterprise.tasks) == baseline


def test_materials_and_completed_sse_survive_reload(api):
    client, app, *_ = api
    cid = client.post("/api/qa/conversations").json()["id"]
    path = f"/api/qa/conversations/{cid}"
    content = "员工信息传输至境外".encode()
    material = client.post(path + "/materials", files={"file": ("员工.txt", content, "text/plain")}).json()
    assert client.get(path + f"/materials/{material['id']}/original").content == content
    assert client.post(path + "/materials", files={"file": ("empty.txt", b"", "text/plain")}).status_code == 422
    enqueue(client, cid, material_ids=[material["id"]])
    run = completed(app.state.qa_store, cid)
    restored = client.get(path).json()
    assert restored["runs"][0]["answer"] == "需根据具体业务判断。"
    assert "parsed_text" not in restored["materials"][0]
    assert "state" not in restored["runs"][0]
    stream = client.get(path + f"/runs/{run['id']}/events")
    assert "event: snapshot" in stream.text and '"status": "completed"' in stream.text


def test_handoff_only_on_submit_copies_originals_without_review(api):
    client, app, cases, enterprise = api
    cid = client.post("/api/qa/conversations").json()["id"]
    path = f"/api/qa/conversations/{cid}"
    content = "客户信息".encode()
    material = client.post(path + "/materials", files={"file": ("facts.txt", content, "text/plain")}).json()
    enqueue(client, cid, material_ids=[material["id"]])
    run = completed(app.state.qa_store, cid)
    assert cases.cases == {} and enterprise.tasks == {}
    payload = {"question": "请正式审查", "material_text": "材料以附件形式提供：facts.txt", "run_id": run["id"],
               "request_id": "case-1", "material_ids": [material["id"]], "intake": {}}
    response = client.post(path + "/cases", json=payload)
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["case"]["status"] == "draft" and not saved["case"]["facts_confirmed"]
    version = saved["imported_versions"][0]
    assert version["parsed_text"] == "客户信息"
    assert app.state.material_object_store.get_original(version["object_key"]) == content
    assert any(e["event_type"] == "qa_handoff" for e in saved["events"])
    assert enterprise.tasks == {} and enterprise.intake_snapshots == {}
    assert client.post(path + "/cases", json=payload).json()["case"]["id"] == saved["case"]["id"]
    assert len(cases.cases) == 1
    assert client.post(path + "/cases", json={**payload, "question": "改变"}).status_code == 409
    assert client.post(path + "/cases", json={**payload, "material_ids": ["foreign"]}).status_code == 422


def test_retry_and_stale_attempt_cannot_overwrite_result():
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    run = store.enqueue(cid, question="问题", material_ids=[], request_id="1")
    claimed = store.claim()["run"]
    store.checkpoint(cid, run["id"], claimed["attempt"], answer="部分", status="failed")
    store.retry(cid, run["id"])
    newer = store.claim()["run"]
    with pytest.raises(QAConflict):
        store.checkpoint(cid, run["id"], claimed["attempt"], answer="旧执行")
    store.checkpoint(cid, run["id"], newer["attempt"], status="completed", answer="新回答")
    assert store.get(cid)["runs"][0]["answer"] == "新回答"
    with pytest.raises(QAConflict):
        store.retry(cid, run["id"])


def test_expired_lease_becomes_retryable():
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    run = store.enqueue(cid, question="问题", material_ids=[], request_id="1")
    store.claim()
    store.rows[cid]["runs"][0]["lease_expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    assert store.claim() is None
    assert store.get(cid)["runs"][0]["status"] == "failed"
    store.retry(cid, run["id"])
    assert store.claim()["run"]["attempt"] == 2


class StreamingClient(FakeClient):
    def stream_text(self, messages, **kwargs):
        self.answer_messages = messages
        self.answer_kwargs = kwargs
        yield "根据具体业务"
        registry = json.loads(messages[-1].content).get("observation", {}).get("citation_registry", [])
        yield "判断。" + (registry[0]["citation_marker"] if registry else "")


class Tools:
    def evidence_as_of(self, evidence, as_of):
        return evidence

    def search(self, queries, facts, *, coarse_scope=False, excluded_scope_terms=()):
        return [_hit()]

    def search_navigation(self):
        return []

    def read_evidence(self, *args, **kwargs):
        from law_agent.review.agent import EvidenceRead
        return EvidenceRead(hits=[_hit()])

    def search_web(self, *args):
        return []


def test_autonomous_search_and_read_direct_stream_without_review(monkeypatch):
    monkeypatch.setattr("law_agent.review.semantic_grounding.SemanticGroundingVerifier.__call__",
                        lambda *_a, **_k: pytest.fail("问答不得调用独立核验"))
    client = StreamingClient(outputs=[
        tool_message(("call_1", 'search_evidence', {'summary': '检索', 'queries': [{'query_id': 'q1', 'text': '数据出境', 'query_type': 'legal_issue'}]})),
        tool_message(("call_2", 'read_evidence', {'summary': '精读', 'read_requests': [{'source_id': 's1', 'chunk_id': 'c1'}]})),
        tool_message(("call_3", 'finish', {'summary': '完成', 'chunk_ids': ['c1']})),
    ])
    updates = []
    store = InMemoryQAStore()
    conversation = store.create("owner")
    run = store.enqueue(conversation["id"], question="什么条件", material_ids=[], request_id="1")
    execute_qa(store.get(conversation["id"]), run, checkpoint=lambda **changes: updates.append(changes), chunks_path="unused", client=client, tools=Tools())
    assert updates[-1]["status"] == "completed"
    assert updates[-1]["answer"] == "根据具体业务判断。[依据1]"
    assert updates[-1]["result"]["conditions"] == []
    assert updates[-1]["result"]["citations"][0]["ref"] == "依据1"
    assert all("report_draft" not in u.get("state", {}) for u in updates)
    assert len(client.calls) == 3
    assert "conditions" not in json.dumps(client.kwargs[0]["tools"], ensure_ascii=False)


def test_qa_selected_web_count_reaches_provider_without_semantic_review(monkeypatch):
    from law_agent.review.web_research import WebFinding

    monkeypatch.setattr("law_agent.review.semantic_grounding.SemanticGroundingVerifier.__call__",
                        lambda *_a, **_k: pytest.fail("问答不得调用独立核验"))
    limits = []

    class WebTools(Tools):
        def search_web(self, queries, facts, max_results):
            limits.append(max_results)
            return [WebFinding(title="公开资料", url="https://example.org/source", excerpt="搜索摘录")]

    client = StreamingClient(outputs=[
        tool_message(("web", "search_web", {"summary": "比较不同来源", "queries": [
            {"query_id": "q1", "query_type": "legal_issue", "text": "适用关系"},
        ], "max_results": 6})),
        tool_message(("finish", "finish", {"summary": "完成", "web_urls": ["https://example.org/source"]})),
    ])
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    run = store.enqueue(cid, question="适用关系", material_ids=[], request_id="1")
    updates = []
    execute_qa(store.get(cid), run, checkpoint=lambda **changes: updates.append(changes),
               chunks_path="unused", client=client, tools=WebTools())
    assert limits == [6]
    assert updates[-1]["status"] == "completed"
    assert updates[-1]["result"]["citations"][0]["kind"] == "web"
    assert updates[-1]["result"]["citations"][0]["can_cite_clause"] is False
    state = next(update["state"] for update in reversed(updates) if "state" in update)
    assert state["steps"][0]["observation"]["max_results"] == 6
    assert len(client.calls) == 2


@pytest.mark.parametrize("fragments", [
    [" \n<", "｜｜DS", "ML｜｜ calls>", "不应显示"],
    ["<|", "DSML| invoke name=\"finish\">"],
    ["<||DSML|| calls>"],
    ["<", "｜｜DSM"],
])
def test_raw_tool_answer_fails_without_publishing_fragments_or_retry(monkeypatch, fragments):
    from itertools import count
    from types import SimpleNamespace

    ticks = count()
    monkeypatch.setattr("law_agent.review.qa_agent.time", SimpleNamespace(monotonic=lambda: next(ticks)))

    class RawClient(FakeClient):
        def stream_text(self, messages, **kwargs):
            yield from fragments

    client = RawClient(outputs=[tool_message(("call_1", "finish", {"summary": "完成"}))])
    updates = []

    def execute(conversation, run, *, checkpoint, **kwargs):
        def recorded_checkpoint(**changes):
            updates.append(changes)
            checkpoint(**changes)
        execute_qa(conversation, run, checkpoint=recorded_checkpoint, client=client, tools=Tools(), **kwargs)

    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    store.enqueue(cid, question="问题", material_ids=[], request_id="1")
    QAWorker(store, chunks_path="unused", execute=execute).run_once()
    run = store.get(cid)["runs"][0]
    assert run["status"] == "failed" and run["answer"] == ""
    assert not any("answer" in update for update in updates)
    assert len(client.calls) == 1


@pytest.mark.parametrize("fragments", [
    ["**结论", "**：需结合条件判断。"],
    ["<", "p>条件说明</p>"],
    ["<", " 5不是适用门槛。"],
])
def test_normal_answer_prefix_is_streamed_without_text_changes(fragments):
    class PlainClient(FakeClient):
        def stream_text(self, messages, **kwargs):
            yield from fragments

    client = PlainClient(outputs=[tool_message(("call_1", "finish", {"summary": "完成"}))])
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    run = store.enqueue(cid, question="问题", material_ids=[], request_id="1")
    updates = []
    execute_qa(store.get(cid), run, checkpoint=lambda **changes: updates.append(changes),
               chunks_path="unused", client=client, tools=Tools())
    assert updates[-1]["status"] == "completed"
    assert updates[-1]["answer"] == "".join(fragments)


def test_answer_keeps_original_context_and_registers_only_selected_citations():
    selected = _hit().model_copy(update={
        "source_id": "current", "title": "现行规则", "publish_date": "2025-01-01",
        "effective_date": "2025-02-01", "article_no": "第二条", "law_status": "effective",
        "text": "主体和行业适用范围", "full_article_text": "完整的主体和行业适用范围及例外",
    })
    unselected = _hit().model_copy(update={"chunk_id": "old", "source_id": "old",
                                         "text": "旧版不同范围", "title": "旧版规则"})

    class MixedTools(Tools):
        def search(self, queries, facts, *, coarse_scope=False, excluded_scope_terms=()):
            return [selected, unselected]

    client = StreamingClient(outputs=[
        tool_message(("call_1", 'read_material', {'summary': '读取用户陈述'})),
        tool_message(("call_2", 'search_evidence', {'summary': '查找', 'queries': [{'query_id': 'q1', 'text': '适用范围', 'query_type': 'legal_issue'}]})),
        tool_message(("call_3", 'finish', {'summary': '回答', 'chunk_ids': ['c1']})),
    ])
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    store.add_material(cid, {"id": "m1", "filename": "业务.txt", "parsed_text": "用户称在当地经营"})
    run = store.enqueue(cid, question="能否适用？", material_ids=["m1"], request_id="1")
    updates = []
    execute_qa(store.get(cid), run, checkpoint=lambda **changes: updates.append(changes),
               chunks_path="unused", client=client, tools=MixedTools())
    messages = client.answer_messages
    assert messages[:len(client.calls[-1])] == client.calls[-1]
    assert messages[0] == client.calls[0][0] and messages[0].content.startswith(PROMPT)
    assert json.loads(messages[1].content)["current_date"] == business_today().isoformat()
    assert json.loads(messages[1].content)["state"]["goal"] == run["question"]
    assert sum(m.role == "user" for m in messages) == sum(m.role == "user" for m in client.calls[-1])
    assert all(m.reasoning_content == "private reasoning" for m in messages if m.role == "assistant")
    observations = [json.loads(m.content) for m in messages if m.role == "tool"]
    assert any("旧版不同范围" in m.content for m in messages if m.role == "tool")
    assert "用户称在当地经营" in observations[0]["observation"]["text"]
    assert any(hit.get("full_article_text") == selected.full_article_text
               for hit in observations[1]["state"]["evidence"])
    assert observations[-1]["observation"]["ready_to_answer"] is True
    registry = observations[-1]["observation"]["citation_registry"]
    assert len(registry) == 1
    assert registry[0] == {
        "citation_marker": citation_marker("chunk:c1"), "chunk_id": "c1", "url": selected.source_url,
        "title": selected.title, "article_no": selected.article_no, "kind": "legal",
        "citation_role": selected.citation_role, "can_cite_clause": selected.can_cite_clause,
        "retrieved_as_of_dates": [business_today().isoformat()],
        "answer_as_of_date": business_today().isoformat(), "available_at_answer_date": True,
    }
    citation = updates[-1]["result"]["citations"][0]
    assert citation["source_id"] == "current" and citation["effective_date"] == "2025-02-01"
    assert citation["article_no"] == "第二条" and citation["excerpt"] == selected.full_article_text
    for field in ("doc_type", "authority", "citation_role", "can_cite_clause"):
        assert citation[field] == getattr(selected, field)
    assert client.answer_kwargs["tools"] == client.kwargs[0]["tools"]
    assert updates[-1]["status"] == "completed" and len(client.calls) == 3


def test_unread_directory_reference_is_returned_to_agent_before_streaming():
    class DirectoryClient(StreamingClient):
        def stream_text(self, messages, **kwargs):
            self.answer_messages = messages
            yield "根据正文判断。" + citation_marker("chunk:c2")

    class DirectoryTools(Tools):
        def read_evidence(self, *args, **kwargs):
            from law_agent.review.agent import EvidenceRead
            return EvidenceRead(hits=[_hit().model_copy(update={"chunk_id": "c2"})])

    client = DirectoryClient(outputs=[
        tool_message(("call_1", 'search_evidence', {'summary': '发现来源', 'queries': [{'query_id': 'q1', 'text': '条件', 'query_type': 'legal_issue'}]})),
        tool_message(("call_2", 'finish', {'summary': '完成', 'chunk_ids': ['c2']})),
        tool_message(("call_3", 'read_evidence', {'summary': '取得正文', 'read_requests': [{'source_id': 's1', 'chunk_id': 'c2'}]})),
        tool_message(("call_4", 'finish', {'summary': '完成', 'chunk_ids': ['c2']})),
    ])
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    run = store.enqueue(cid, question="什么条件", material_ids=[], request_id="1")
    updates = []
    execute_qa(store.get(cid), run, checkpoint=lambda **changes: updates.append(changes),
               chunks_path="unused", client=client, tools=DirectoryTools())
    assert updates[-1]["status"] == "completed"
    steps = [u["state"]["steps"] for u in updates if "state" in u][-1]
    assert steps[1]["action"] == "finish"
    assert "c2" in steps[1]["observation"]["error"]
    assert steps[2]["action"] == "read_evidence"
    assert len(client.calls) == 4


def test_invalid_citation_at_budget_limit_delivers_limited_answer(monkeypatch):
    class PlainClient(FakeClient):
        def stream_text(self, messages, **kwargs):
            self.answer_messages = messages
            yield "资料不足，目前无法确定。"

    monkeypatch.setattr("law_agent.review.qa_agent.QAState", lambda **kwargs: QAState(**kwargs, max_turns=1))
    client = PlainClient(outputs=[tool_message(("call_1", 'finish', {'summary': '完成', 'chunk_ids': ['unread']}))])
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    run = store.enqueue(cid, question="什么条件", material_ids=[], request_id="1")
    updates = []
    execute_qa(store.get(cid), run, checkpoint=lambda **changes: updates.append(changes),
               chunks_path="unused", client=client, tools=Tools())
    assert updates[-1]["status"] == "completed"
    assert updates[-1]["result"]["citations"] == []
    assert updates[-1]["result"]["conditions"] == []
    assert "达到执行上限" in json.loads(client.answer_messages[-1].content)["evidence_gaps"][0]
    assert updates[-1]["result"]["budget_exhausted"] is True
    assert "达到执行上限" in updates[-1]["result"]["evidence_gaps"][0]
    assert updates[-1]["result"]["generation_status"] == "completed"
    assert len(client.calls) == 1


@pytest.mark.parametrize("decision,materials", [
    ({"chunk_ids": ["forged"]}, []),
    ({"web_urls": ["https://forged.example"]}, []),
    ({"material_citations": [{"material_version_id": "foreign", "quote": "原文"}]}, []),
    ({"material_citations": [{"material_version_id": "m1", "quote": "不存在"}]}, [{"id": "m1", "parsed_text": "原文"}]),
])
def test_citation_identifiers_and_material_quotes_must_exist(decision, materials):
    with pytest.raises(ValueError):
        prepare_citations(QAState(goal="q"), QADecision(action="finish", summary="结束", **decision), materials)


def test_worker_failure_retains_partial_answer_for_reload():
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    store.enqueue(cid, question="问题", material_ids=[], request_id="1")
    def execute(_conversation, _run, *, checkpoint, **kwargs):
        checkpoint(answer="已生成的部分")
        raise RuntimeError("provider disconnected")
    QAWorker(store, chunks_path="unused", execute=execute).run_once()
    run = store.get(cid)["runs"][0]
    assert run["status"] == "failed" and run["answer"] == "已生成的部分"
    assert run["result"]["generation_status"] == "failed"


def test_budget_and_evidence_gaps_survive_answer_stream_failure(monkeypatch):
    class InterruptedClient(FakeClient):
        def stream_text(self, messages, **kwargs):
            yield "未完成的确定性结论"
            raise RuntimeError("disconnected")

    monkeypatch.setattr("law_agent.review.qa_agent.QAState", lambda **kwargs: QAState(**kwargs, max_turns=0))
    client = InterruptedClient(outputs=[])
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    store.enqueue(cid, question="问题", material_ids=[], request_id="1")
    def execute(conversation, run, **kwargs):
        return execute_qa(conversation, run, client=client, tools=Tools(), **kwargs)
    QAWorker(store, chunks_path="unused", execute=execute).run_once()
    result = store.get(cid)["runs"][0]["result"]
    assert result["generation_status"] == "failed"
    assert result["budget_exhausted"] is True
    assert "达到执行上限" in result["evidence_gaps"][0]


def test_finish_evidence_gap_is_persisted_without_budget_exhaustion():
    client = StreamingClient(outputs=[tool_message(("finish", "finish", {
        "summary": "条件性回答", "evidence_gaps": ["缺少适用规则全文"]}))])
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    run = store.enqueue(cid, question="问题", material_ids=[], request_id="1")
    updates = []
    execute_qa(store.get(cid), run, checkpoint=lambda **kw: updates.append(kw),
               chunks_path="unused", client=client, tools=Tools())
    result = updates[-1]["result"]
    assert result["budget_exhausted"] is False
    assert result["evidence_gaps"] == ["缺少适用规则全文"]
    assert result["generation_status"] == "completed"


def test_expired_lease_marks_partial_result_failed_without_discarding_limits():
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    store.enqueue(cid, question="问题", material_ids=[], request_id="1")
    run = store.claim()["run"]
    store.checkpoint(cid, run["id"], run["attempt"], answer="部分输出", result={
        "evidence_gaps": ["缺少原文"], "budget_exhausted": True, "generation_status": "streaming"})
    store.rows[cid]["runs"][0]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
    assert store.claim() is None
    saved = store.get(cid)["runs"][0]
    assert saved["status"] == saved["result"]["generation_status"] == "failed"
    assert saved["answer"] == "部分输出"
    assert saved["result"]["evidence_gaps"] == ["缺少原文"]


def test_worker_persists_native_answer_stream_usage_without_exposing_it(api, monkeypatch):
    client, app, *_ = api
    usage = {"prompt_tokens": 100, "prompt_cache_hit_tokens": 75, "prompt_cache_miss_tokens": 25, "completion_tokens": 20}

    def respond(request, **kwargs):
        payload = json.loads(request.data)
        if payload.get("stream"):
            return BytesIO(('data: ' + json.dumps({"choices": [{"delta": {"content": "资料不足，无法确定。"},
                "finish_reason": "stop"}], "usage": usage}) + '\n\ndata: [DONE]\n\n').encode())
        return BytesIO(json.dumps({"choices": [{"finish_reason": "tool_calls", "message": tool_message(
            ("finish", "finish", {"summary": "已有资料不足", "evidence_gaps": ["缺少本轮适用的原文"]}))}], "usage": usage}).encode())

    monkeypatch.setattr("urllib.request.urlopen", respond)
    config = LLMConfig(api_key="test", base_url="https://example.test", beta_base_url="https://example.test/beta",
                       model="test", timeout_seconds=10, structured_output_mode="json_object", reasoning_effort="low")
    cid = client.post("/api/qa/conversations").json()["id"]
    enqueue(client, cid)

    def execute(conversation, run, **kwargs):
        execute_qa(conversation, run, client=OpenAICompatibleClient(config), tools=Tools(), **kwargs)

    QAWorker(app.state.qa_store, chunks_path="unused", execute=execute).run_once()
    saved = app.state.qa_store.get(cid)["runs"][0]
    assert saved["status"] == "completed"
    assert [r["phase"] for r in saved["state"]["usage"]] == ["qa_investigation"]
    assert all(r["cache_hit_tokens"] == 75 for r in saved["state"]["usage"])
    public = client.get(f"/api/qa/conversations/{cid}").json()["runs"][0]
    assert "state" not in public and "usage" not in public
    assert public["result"]["conditions"] == []


def test_completed_product_question_cannot_create_review_actions_or_approval(api, monkeypatch):
    client, app, cases, enterprise = api
    before = deepcopy((cases.cases, cases.events, cases.remediation_plans, cases.remediation_tasks,
                       enterprise.tasks, enterprise.snapshots, enterprise.intake_snapshots))
    monkeypatch.setattr("law_agent.review.remediation.reconcile_guided_action_list",
                        lambda *_a, **_k: pytest.fail("问答不能生成整改事项"))
    monkeypatch.setattr("law_agent.review.semantic_grounding.SemanticGroundingVerifier.__call__",
                        lambda *_a, **_k: pytest.fail("问答不能进入正式核验"))
    cid = client.post("/api/qa/conversations").json()["id"]
    enqueue(client, cid)

    def execute(conversation, run, **kwargs):
        execute_qa(conversation, run, client=StreamingClient(outputs=[
            tool_message(("call_1", 'search_evidence', {'summary': '查找', 'queries': [{'query_id': 'q1', 'query_type': 'legal_issue', 'text': '条件'}]})),
            tool_message(("call_2", 'finish', {'summary': '回答', 'chunk_ids': ['c1']})),
        ]), tools=Tools(), **kwargs)

    QAWorker(app.state.qa_store, chunks_path=app.state.chunks_path, execute=execute).run_once()
    response = client.get(f"/api/qa/conversations/{cid}").json()["runs"][0]
    assert response["status"] == "completed"
    assert (cases.cases, cases.events, cases.remediation_plans, cases.remediation_tasks,
            enterprise.tasks, enterprise.snapshots, enterprise.intake_snapshots) == before
    assert "review_result" not in response["result"]


def test_streaming_client_yields_content_and_rejects_truncation(monkeypatch):
    class Response(BytesIO):
        pass
    packets = b'data: {"choices":[{"delta":{"content":"hello"},"finish_reason":null}]}\n\ndata: [DONE]\n\n'
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: Response(packets))
    client = OpenAICompatibleClient(LLMConfig(api_key="test", base_url="https://example.test/v1", beta_base_url="https://example.test/beta", model="test", timeout_seconds=10, structured_output_mode="json_object", reasoning_effort="none"))
    assert list(client.stream_text([ChatMessage(role="user", content="hi")])) == ["hello"]
    packets = b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":"length"}]}\n\n'
    with pytest.raises(RuntimeError, match="length"):
        list(client.stream_text([ChatMessage(role="user", content="hi")]))


@pytest.mark.parametrize("effort", ["none", "low"])
def test_streaming_respects_configured_reasoning_without_exposing_it(monkeypatch, effort):
    requests = []
    packets = (b'data: {"choices":[{"delta":{"reasoning_content":"private"}}]}\n\n'
               b'data: {"choices":[{"delta":{"content":"answer"},"finish_reason":"stop"}]}\n\n'
               b'data: [DONE]\n\n')

    def respond(request, **kwargs):
        requests.append(json.loads(request.data))
        return BytesIO(packets)

    monkeypatch.setattr("urllib.request.urlopen", respond)
    config = LLMConfig(api_key="test", base_url="https://example.test/v1", beta_base_url="https://example.test/beta",
                       model="test", timeout_seconds=10, structured_output_mode="json_object", reasoning_effort=effort)
    assert list(OpenAICompatibleClient(config).stream_text([ChatMessage(role="user", content="hi")])) == ["answer"]
    payload = requests[0]
    assert payload["thinking"] == {"type": "disabled" if effort == "none" else "enabled"}
    if effort == "none":
        assert "reasoning_effort" not in payload
        assert payload["temperature"] == 0
    else:
        assert payload["reasoning_effort"] == effort
        assert "temperature" not in payload


def test_answer_stream_rejects_unexpected_tool_deltas(monkeypatch):
    packets = b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"unexpected"}]}}]}\n\n'
    monkeypatch.setattr("urllib.request.urlopen", lambda *a, **k: BytesIO(packets))
    config = LLMConfig(api_key="test", base_url="https://example.test/v1", beta_base_url="https://example.test/beta",
                       model="test", timeout_seconds=10, structured_output_mode="json_object", reasoning_effort="low")
    with pytest.raises(RuntimeError, match="unexpected tool call"):
        list(OpenAICompatibleClient(config).stream_text([ChatMessage(role="user", content="问题")], tools=[]))


def test_clarification_cannot_be_handed_to_formal_review(api):
    client, app, *_ = api
    cid = client.post("/api/qa/conversations").json()["id"]
    store = app.state.qa_store
    store.enqueue(cid, question="是否需要申报", material_ids=[], request_id="1")
    run = store.claim()["run"]
    store.checkpoint(cid, run["id"], run["attempt"], status="completed", answer="请补充事实",
                     result={"questions": [{"question": "实际累计人数？", "selection": "single", "options": ["已统计", "未统计"]}], "citations": [], "conditions": []})
    response = client.post(f"/api/qa/conversations/{cid}/cases", json={
        "run_id": run["id"], "request_id": "handoff", "question": "请正式审查", "material_ids": [],
        "material_text": "待补充业务事实", "intake": {}})
    assert response.status_code == 409
    assert "补充业务事实" in response.json()["detail"]
