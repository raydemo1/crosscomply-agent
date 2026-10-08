"""Explicit confirmation, frozen versions and rollback without model calls."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from law_agent.review.agent import AgentState
from law_agent.review.api import create_app
from law_agent.review.case_store import InMemoryCaseStore
from law_agent.review.enterprise_store import InMemoryEnterpriseStore
from law_agent.review.fact_confirmation import confirm_facts, stage_fact_answer
from law_agent.review.fact_provenance import confirmed_intake_ledger
from law_agent.review.http.schemas import FactAnswerRequest, FactConfirmRequest, IntakePayload


def make_matter(tmp_path):
    cases = InMemoryCaseStore(seed_password="pw")
    enterprise = InMemoryEnterpriseStore()
    user = cases.authenticate("requester@crosscomply.local", "pw")
    intake = IntakePayload(cross_border_transfer=True).model_dump(mode="json")
    case = cases.create_case(question="判断出境路径", material_text="客户信息传输至境外", intake=intake, created_by=user.id, status="needs_info")
    version = enterprise.create_material_version(
        case_id=case["id"], logical_name="业务说明", filename="facts.txt", content_type="text/plain",
        object_key="test/facts.txt", sha256="a" * 64, byte_size=20, uploaded_by=user.id,
        parse_status="ready", parsed_text="客户信息传输至境外",
    )
    material = enterprise.create_material_snapshot(case_id=case["id"], version_ids=[version.id], created_by=user.id)
    frozen = enterprise.create_intake_snapshot(case_id=case["id"], material_snapshot_id=material.id, intake=intake, created_by=user.id)
    task = enterprise.enqueue_review_task(case_id=case["id"], material_snapshot_id=material.id, intake_snapshot_id=frozen.id, model_id="fake", data_boundary_summary={})
    enterprise.claim_next_task(worker_id="fake")
    state = AgentState(
        goal="审查", status="waiting_input", gate_id="gate", pending_question="请确认目的地和统计期间",
        turns=16, max_turns=16,
        fact_questions=[{"field": "destination_region", "answer_type": "text"}, {"field": "count_period", "answer_type": "choice"}],
        fact_ledger=confirmed_intake_ledger(intake, frozen.id),
    )
    enterprise.pause_task(task.id, state=state.model_dump(mode="json"))
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text("", encoding="utf-8")
    app = create_app(chunks_path=chunks, case_store=cases, enterprise_store=enterprise)
    return SimpleNamespace(cases=cases, enterprise=enterprise, user=user, case=case, material=material, intake=frozen, task=task, app=app)


@pytest.fixture
def matter(tmp_path):
    return make_matter(tmp_path)


def binding(m):
    return {"task_id": m.task.id, "material_snapshot_id": m.material.id, "intake_snapshot_id": m.intake.id}


def stage(m, **changes):
    payload = FactAnswerRequest(**{**binding(m), "gate_id": "gate", "answer": "接收地为日本", "values": {"destination_region": "日本"}, **changes})
    return stage_fact_answer(cases=m.cases, enterprise=m.enterprise, user=m.user, case_id=m.case["id"], payload=payload)


def confirmation(m, **changes):
    state = m.enterprise.get_task(m.task.id).agent_state
    return FactConfirmRequest(**{**binding(m), "gate_id": "gate", "answer_revision": state["fact_answer_revision"], "values": state["pending_fact_values"], "confirmed": True, **changes})


def confirm(m, payload):
    return confirm_facts(cases=m.cases, enterprise=m.enterprise, user=m.user, case_id=m.case["id"], payload=payload)


def test_staging_remains_unverified_and_does_not_consume_agent_budget(matter):
    m = matter
    first = stage(m)
    second = stage(m, answer="更正：新加坡", values={"destination_region": "新加坡"})
    state = second["agent_state"]
    assert state["status"] == "waiting_input" and state["turns"] == 16
    assert first["agent_state"]["fact_answer_revision"] != state["fact_answer_revision"]
    assert m.cases.get_case(m.case["id"])["intake"] == m.intake.intake
    assert len(m.enterprise.intake_snapshots) == 1
    statements = [e for e in state["fact_ledger"] if e["source_type"] == "applicant_statement"]
    assert statements and all(e["status"] == "unverified" for e in statements)


def test_confirmation_replaces_task_with_new_facts_same_material(matter):
    m = matter
    stage(m)
    request = confirmation(m)
    result = confirm(m, request)
    new = m.enterprise.get_task(result["task_id"])
    assert new.status == "queued" and new.id != m.task.id
    assert new.material_snapshot_id == m.material.id
    assert new.intake_snapshot_id != m.intake.id
    assert m.enterprise.get_task(m.task.id).status == "superseded"
    assert m.intake.intake["destination_region"] == ""
    assert m.cases.get_case(m.case["id"])["intake"]["destination_region"] == "日本"
    assert m.cases.get_case(m.case["id"])["status"] == "review_running"
    with pytest.raises(ValueError, match="版本已变化"):
        confirm(m, request)
    assert len(m.enterprise.tasks) == 2 and len(m.enterprise.intake_snapshots) == 2


def test_confirming_a_historical_value_still_creates_a_new_version_and_task(matter):
    m = matter
    first_intake = m.intake
    stage(m)
    queued = confirm(m, confirmation(m))
    m.task = m.enterprise.get_task(queued["task_id"])
    m.intake = m.enterprise.get_intake_snapshot(m.task.intake_snapshot_id)
    m.enterprise.claim_next_task(worker_id="second")
    m.enterprise.pause_task(m.task.id, state=AgentState(
        goal="复核", status="waiting_input", gate_id="gate", pending_question="再核对接收地",
        fact_questions=[{"field": "destination_region", "answer_type": "text"}],
    ).model_dump(mode="json"))
    m.cases.update_case(m.case["id"], status="needs_info")
    stage(m, values={"destination_region": ""}, answer="原来填的日本不确定，恢复未知")
    queued = confirm(m, confirmation(m))
    restored = m.enterprise.get_intake_snapshot(m.enterprise.get_task(queued["task_id"]).intake_snapshot_id)
    assert restored.intake == first_intake.intake and restored.id != first_intake.id
    assert len(m.enterprise.intake_snapshots) == 3 and len(m.enterprise.tasks) == 3


@pytest.mark.parametrize("change", [{"gate_id": "old"}, {"answer_revision": "old"}, {"values": {"destination_region": "美国"}}, {"material_snapshot_id": "other"}])
def test_stale_confirmation_is_rejected_without_writes(matter, change):
    stage(matter)
    with pytest.raises(ValueError):
        confirm(matter, confirmation(matter, **change))
    assert len(matter.enterprise.tasks) == 1 and len(matter.enterprise.intake_snapshots) == 1
    assert matter.enterprise.get_task(matter.task.id).status == "waiting_input"


def test_enqueue_failure_rolls_back_snapshot_intake_task_and_events(matter, monkeypatch):
    m = matter
    stage(m)
    request = confirmation(m)
    before = deepcopy((m.cases.get_case(m.case["id"]), m.cases.events, m.enterprise.tasks, m.enterprise.intake_snapshots, m.enterprise.task_by_key))
    def fail(**_kwargs):
        raise RuntimeError("queue failed")
    monkeypatch.setattr(m.enterprise, "enqueue_review_task", fail)
    with pytest.raises(RuntimeError, match="queue failed"):
        confirm(m, request)
    assert (m.cases.get_case(m.case["id"]), m.cases.events, m.enterprise.tasks, m.enterprise.intake_snapshots, m.enterprise.task_by_key) == before


def test_intake_drift_and_approval_state_block_confirmation(matter):
    m = matter
    stage(m)
    request = confirmation(m)
    m.cases.update_case(m.case["id"], intake_json=IntakePayload(destination_region="美国").model_dump(mode="json"))
    with pytest.raises(ValueError, match="填报事实已变化"):
        confirm(m, request)
    m.cases.update_case(m.case["id"], intake_json=m.intake.intake, status="approved")
    with pytest.raises(ValueError, match="等待补充事实"):
        confirm(m, request)


@pytest.mark.parametrize("confirmed", [False, 1, "true", None])
def test_confirmation_requires_literal_boolean_consent(matter, confirmed):
    stage(matter)
    with pytest.raises(ValidationError):
        confirmation(matter, confirmed=confirmed)


def test_http_owner_type_guards_and_explicit_confirmation(matter):
    m = matter
    with TestClient(m.app) as client:
        assert client.post("/api/auth/login", json={"username": m.user.username, "password": "pw"}).status_code == 200
        url = f"/api/cases/{m.case['id']}/fact-answers"
        body = {**binding(m), "gate_id": "gate", "answer": "日本", "values": {"destination_region": "日本"}}
        assert client.post(url, json={**body, "values": {"destination_region": 123}}).status_code == 422
        assert client.post(url, json={**body, "values": {"ciio_status": "ciio"}}).status_code == 409
        answer = client.post(url, json=body)
        assert answer.status_code == 200, answer.text
        confirmed = client.post(f"/api/cases/{m.case['id']}/confirm-facts", json=confirmation(m).model_dump(mode="json"))
        assert confirmed.status_code == 202, confirmed.text
        assert client.post("/api/auth/login", json={"username": "reviewer@crosscomply.local", "password": "pw"}).status_code == 200
        assert client.post(url, json=body).status_code == 404
