import json
import threading
import time
from copy import deepcopy
from types import SimpleNamespace

import anyio
import pytest
from fastapi.testclient import TestClient

from law_agent.llm.structured_stream import _AnswerFieldStream
from law_agent.review.http.schemas import IntakePayload, MatterQuestionRequest
from law_agent.review.matter_agent import matter_context
from tests.test_fact_confirmation import binding, make_matter


@pytest.fixture
def matter(tmp_path):
    return make_matter(tmp_path)


def reply():
    return {"answer": "第一段\n第二段", "claims": [], "material_citations": [], "fact_fields": [], "proposed_facts": {}}


def events(text):
    return [json.loads(line[6:]) for line in text.splitlines() if line.startswith("data: ")]


def stream_endpoint(app):
    routes = list(app.routes)
    while routes:
        route = routes.pop()
        if getattr(route, 'path', '').endswith('/conversation/stream'):
            return route.endpoint
        routes.extend(getattr(getattr(route, 'original_router', None), 'routes', []))
    raise AssertionError('Matter stream route missing')


@pytest.mark.parametrize("answer", ['中文\n换行\t制表符，"引用"与\\斜杠', '中😀文'])
@pytest.mark.parametrize("ascii_only", [True, False])
def test_answer_extraction_handles_every_split_and_never_leaks_other_fields(answer, ascii_only):
    text = json.dumps({"claims": [{"text": "nested answer", "answer": "not the answer"}], "answer": answer, "more": "private"}, ensure_ascii=ascii_only)
    for split in range(len(text) + 1):
        parser = _AnswerFieldStream()
        assert parser.feed(text[:split]) + parser.feed(text[split:]) + parser.feed('extra') == answer
    parser = _AnswerFieldStream()
    assert ''.join(parser.feed(char) for char in text) == answer


def test_stream_delivers_delta_before_completion_and_saves_once(matter):
    m = matter
    released = threading.Event()
    completed = threading.Event()
    def answer(question, context, *, on_event):
        on_event({"type": "delta", "text": "第一段"})
        assert released.wait(3)
        completed.set()
        on_event({"type": "delta", "text": "\n第二段"})
        return reply()
    m.app.state.matter_stream_answerer = answer
    endpoint = stream_endpoint(m.app)
    before = deepcopy((m.cases.cases, m.enterprise.tasks))
    async def connected():
        return False
    async def consume():
        response = await endpoint(m.case['id'], MatterQuestionRequest(**binding(m), question="为什么"), SimpleNamespace(is_disconnected=connected), m.user)
        received = []
        async for block in response.body_iterator:
            for event in events(block):
                received.append(event)
                if event['type'] == 'delta' and not released.is_set():
                    assert not completed.is_set()
                    assert not [e for e in m.cases.list_events(m.case['id']) if e['event_type'] == 'matter_agent_turn']
                    released.set()
        return received
    received = anyio.run(consume)
    assert [e['type'] for e in received] == ['status', 'delta', 'delta', 'done']
    turn = received[-1]['turn']
    assert turn['reply'] == reply() and turn['id']
    saved = [e for e in m.cases.list_events(m.case['id']) if e['event_type'] == 'matter_agent_turn']
    assert len(saved) == 1 and saved[0]['id'] == turn['id']
    assert (m.cases.cases, m.enterprise.tasks) == before


@pytest.mark.parametrize('failure', ['model', 'version', 'permission', 'timeout'])
def test_stream_errors_end_without_persisting_a_turn(matter, failure):
    m = matter
    def answer(question, context, *, on_event):
        on_event({'type': 'delta', 'text': '部分正文'})
        if failure == 'model':
            raise RuntimeError('provider error with private details')
        if failure == 'version':
            m.enterprise.create_intake_snapshot(case_id=m.case['id'], material_snapshot_id=m.material.id, intake=IntakePayload(destination_region='日本').model_dump(mode='json'), created_by=m.user.id)
        if failure == 'permission':
            m.cases.update_case(m.case['id'], owner_id='someone-else', created_by='someone-else')
        if failure == 'timeout':
            time.sleep(0.15)
        return reply()
    m.app.state.matter_stream_answerer = answer
    if failure == 'timeout':
        m.app.state.matter_stream_timeout = 0.02
    with TestClient(m.app) as client:
        client.post('/api/auth/login', json={'username': m.user.username, 'password': 'pw'})
        response = client.post(f"/api/cases/{m.case['id']}/conversation/stream", json={**binding(m), 'question': '解释'})
    received = events(response.text)
    assert received[-1]['type'] == 'error'
    assert 'private details' not in response.text
    time.sleep(0.2)
    assert not [e for e in m.cases.list_events(m.case['id']) if e['event_type'] == 'matter_agent_turn']


def test_disconnected_stream_does_not_save(matter):
    m = matter
    disconnected = False
    def answer(question, context, *, on_event):
        on_event({'type': 'delta', 'text': '部分正文'})
        time.sleep(0.1)
        return reply()
    m.app.state.matter_stream_answerer = answer
    endpoint = stream_endpoint(m.app)
    async def is_disconnected():
        return disconnected
    async def consume():
        nonlocal disconnected
        response = await endpoint(m.case['id'], MatterQuestionRequest(**binding(m), question='解释'), SimpleNamespace(is_disconnected=is_disconnected), m.user)
        async for block in response.body_iterator:
            if any(e['type'] == 'delta' for e in events(block)):
                disconnected = True
    anyio.run(consume)
    time.sleep(0.2)
    assert not [e for e in m.cases.list_events(m.case['id']) if e['event_type'] == 'matter_agent_turn']


def test_selection_is_validated_and_saved_with_server_label(matter):
    m = matter
    version = m.enterprise.get_material_version(m.material.version_ids[0])
    selection = {'kind': 'material', 'quote': '客户信息', 'material_version_id': version.id, 'start_offset': 0, 'end_offset': 4}
    request = MatterQuestionRequest(**binding(m), question='解释', selection=selection)
    assert matter_context(m.cases, m.enterprise, m.case['id'], request)['selection']['label'] == 'facts.txt'
    m.app.state.matter_stream_answerer = lambda *args, **kwargs: reply()
    with TestClient(m.app) as client:
        client.post('/api/auth/login', json={'username': m.user.username, 'password': 'pw'})
        response = client.post(f"/api/cases/{m.case['id']}/conversation/stream", json=request.model_dump(mode='json'))
        turn = events(response.text)[-1]['turn']
        assert turn['selection'] == {**selection, 'label': 'facts.txt'}
        assert client.get(f"/api/cases/{m.case['id']}/conversation").json()['items'][0] == turn
        for changes in [{'quote': '伪造'}, {'end_offset': 3}, {'material_version_id': 'other'}]:
            response = client.post(f"/api/cases/{m.case['id']}/conversation/stream", json={**binding(m), 'question': '解释', 'selection': {**selection, **changes}})
            assert response.status_code == 409
        assert client.post('/api/cases/missing/conversation/stream', json={**binding(m), 'question': '解释'}).status_code == 404
    with TestClient(m.app) as client:
        assert client.post(f"/api/cases/{m.case['id']}/conversation/stream", json={**binding(m), 'question': '解释'}).status_code == 401


def test_report_selection_requires_current_report(matter):
    m = matter
    request = MatterQuestionRequest(**binding(m), question='解释', selection={'kind': 'report', 'quote': '用户选摘', 'review_result_id': 'old'})
    with pytest.raises(ValueError, match='报告已变化'):
        matter_context(m.cases, m.enterprise, m.case['id'], request)
