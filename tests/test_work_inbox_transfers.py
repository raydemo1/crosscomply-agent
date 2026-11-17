"""Work inbox and version-bound legal review of transfer changes."""

from datetime import timedelta

from fastapi.testclient import TestClient

from law_agent.review.api import create_app
from law_agent.review.case_store import InMemoryCaseStore, UserRecord
from law_agent.review.dates import business_today
from law_agent.review.enterprise_store import InMemoryEnterpriseStore
from law_agent.review.work_reminders import WorkReminderWorker


def make_app(chunks_path):
    return create_app(
        chunks_path=chunks_path, case_store=InMemoryCaseStore(seed_password="test-password"),
        enterprise_store=InMemoryEnterpriseStore(),
    )


def user_for(app, role):
    return next(user for user in app.state.case_store.list_assignable_users() if user.role == role)


def signed_case(app, owner, *, recipient="新加坡服务商", destination="新加坡"):
    cases = app.state.case_store
    case = cases.create_case(
        title="跨境客服系统", question="评估客服系统境外访问", material_text="现有处理安排",
        created_by=owner.id, owner_id=owner.id,
        intake={"overseas_recipient": recipient, "destination_region": destination,
                "data_types": ["企业客户联系人"]},
    )
    task = review_task(app, case, case["intake"])
    approve(app, case["id"], task.id)
    return case


def review_task(app, case, intake, *, waiting=False):
    enterprise = app.state.enterprise_store
    version = enterprise.create_material_version(
        case_id=case["id"], logical_name="材料", filename="facts.txt", content_type="text/plain",
        object_key=f"test/{case['id']}", sha256="b" * 64, byte_size=12,
        uploaded_by=case["owner_id"], parse_status="ready", parsed_text="拟使用新的供应商",
    )
    snapshot = enterprise.create_material_snapshot(case_id=case["id"], version_ids=[version.id], created_by=case["owner_id"])
    facts = enterprise.create_intake_snapshot(
        case_id=case["id"], material_snapshot_id=snapshot.id, intake=intake, created_by=case["owner_id"],
    )
    queued = enterprise.enqueue_review_task(
        case_id=case["id"], material_snapshot_id=snapshot.id, intake_snapshot_id=facts.id,
        model_id="example-review-model", data_boundary_summary={},
    )
    assert enterprise.claim_next_task(worker_id="test").id == queued.id
    if waiting:
        app.state.case_store.update_case(case["id"], status="needs_info")
        return enterprise.pause_task(queued.id, state={"pending_question": "请确认接收方", "gate_id": "gate_1"})
    result = {"review_result": {"risk_level": "medium"}}
    app.state.case_store.update_case(case["id"], status="pending_feishu_approval", response=result)
    return enterprise.complete_task(queued.id, result=result, final_node="done")


def approve(app, case_id, task_id):
    governance = app.state.governance_store
    approval = governance.create_approval_record(case_id=case_id, task_id=task_id, instance_id=f"instance_{case_id}")
    governance.record_approval_event(
        approval_id=approval.id, provider_event_id=f"event_{case_id}", event_type="approval",
        signature_valid=True, payload={}, target_status="approved", decided_at="2026-11-16T12:00:00+08:00",
    )
    app.state.case_store.update_case(case_id, status="approved")


def client_for(app, user):
    client = TestClient(app)
    assert client.post("/api/auth/login", json={"username": user.username, "password": "test-password"}).status_code == 200
    return client


def test_transfer_review_uses_frozen_facts_and_requires_legal_confirmation(tmp_path):
    app = make_app(tmp_path / "chunks.jsonl")
    owner, reviewer = (user_for(app, role) for role in ("requester", "reviewer"))
    applicant, legal = client_for(app, owner), client_for(app, reviewer)
    original = signed_case(app, owner)
    route = "/api/transfer-activities"
    payload = {"case_id": original["id"], "title": "跨境客服运营", "business_entity": "甲公司",
               "next_review_on": business_today().isoformat()}
    registered = applicant.post(route, json=payload)
    assert registered.status_code == 201, registered.text
    assert applicant.post(route, json=payload).status_code == 409
    url = f"{route}/{registered.json()['id']}"
    assert applicant.get(url).json()["recipient"] == "新加坡服务商"
    assert applicant.get("/api/work-inbox").json()["items"][0]["kind"] == "transfer_due"

    start = {"baseline_case_id": original["id"], "business_entity": "乙公司", "change_note": "更换境外供应商"}
    draft = applicant.post(f"{url}/reviews", json=start)
    assert draft.status_code == 201, draft.text
    assert applicant.post(f"{url}/reviews", json=start).status_code == 409
    case_id = draft.json()["case_id"]
    tasks = applicant.get("/api/work-inbox").json()["items"]
    assert [(item["kind"], item["href"]) for item in tasks] == [("transfer_draft", f"/workbench?case={case_id}")]
    case = app.state.case_store.get_case(case_id)
    assert case["owner_id"] == owner.id and case["intake"]["overseas_recipient"] == "新加坡服务商"
    task = review_task(app, case, {**case["intake"], "overseas_recipient": "东京服务商", "destination_region": "日本"})
    review = legal.get(url).json()["reviews"][0]
    assert review["can_confirm"]
    assert {item["field"] for item in review["changes"]} == {"业务主体", "境外接收方", "接收地"}
    assert legal.get("/api/work-inbox").json()["items"][0]["kind"] == "impact_review"
    assert "确认本次变更" in legal.post(f"/api/cases/{case_id}/feishu-approval").json()["detail"]

    impact = {"case_id": case_id, "task_id": task.id, "outcome": "scope_changed", "note": "接收地和接收方改变"}
    assert applicant.post(f"{url}/impact", json=impact).status_code == 403
    assert legal.post(f"{url}/impact", json={**impact, "task_id": "old_task"}).status_code == 409
    assert legal.post(f"{url}/impact", json=impact).status_code == 200
    assert legal.post(f"{url}/impact", json=impact).status_code == 409
    assert legal.get("/api/work-inbox").json()["items"][0]["kind"] == "approval"
    assert applicant.get(url).json()["approval"]["case_id"] == original["id"]

    approve(app, case_id, task.id)
    current = applicant.get(url).json()
    assert (current["approval"]["case_id"], current["recipient"], current["business_entity"]) == (case_id, "东京服务商", "乙公司")
    assert applicant.post(f"{url}/reviews", json=start).status_code == 409
    assert app.state.case_store.get_case(original["id"])["intake"]["overseas_recipient"] == "新加坡服务商"


def test_inbox_only_shows_actions_the_current_user_can_take(tmp_path):
    app = make_app(tmp_path / "chunks.jsonl")
    owner, reviewer = (user_for(app, role) for role in ("requester", "reviewer"))
    cases = app.state.case_store
    case = cases.create_case(question="核实接收方", material_text="资料", created_by=owner.id, owner_id=owner.id)
    review_task(app, case, {}, waiting=True)
    signed = signed_case(app, owner)
    plan = cases.create_remediation_plan(signed["id"], reviewer.id)
    remediation = cases.create_remediation_task(
        plan["id"], case_id=signed["id"], title="提供培训证明", phase="post_approval", assignee_id=owner.id,
        due_date=(business_today() + timedelta(days=1)).isoformat(),
    )
    applicant, legal = client_for(app, owner), client_for(app, reviewer)
    items = applicant.get("/api/work-inbox").json()["items"]
    assert [(item["kind"], item["urgency"]) for item in items] == [("remediation", "soon"), ("supplement", None)]
    assert legal.get("/api/work-inbox").json()["items"] == []
    cases.start_remediation_task(remediation["id"])
    cases.create_remediation_submission(remediation["id"], submitted_by=owner.id, note="已完成", evidence=[])
    assert [item["kind"] for item in legal.get("/api/work-inbox").json()["items"]] == ["remediation_review"]
    assert [item["kind"] for item in applicant.get("/api/work-inbox").json()["items"]] == ["supplement"]


def test_reminder_retries_same_uuid_and_keeps_business_titles_out(monkeypatch):
    delivered = set()
    calls = []
    user = UserRecord("user_owner", "owner@example.com", "业务负责人", "requester")
    item = {"id": "supplement:gate_1", "notification_user_id": user.id, "urgency": None, "due_on": None,
            "notify_on_ready": True, "title": "未披露供应商", "action": "回答问题",
            "reason": "审查正在等待申报人回答", "href": "/cases/case_test?action=answer"}

    class Client:
        def send_text_message(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise RuntimeError("timeout")

    worker = WorkReminderWorker("postgresql://unused", Client(), "https://work.example.com/", {user.username: "ou_test"})
    monkeypatch.setattr(worker.cases, "list_assignable_users", lambda: [user])
    monkeypatch.setattr(worker.inbox, "list", lambda _: [item, {**item, "notification_user_id": "other_user"}])
    monkeypatch.setattr(worker, "_claim", lambda key: key not in delivered)
    monkeypatch.setattr(worker, "_sent", delivered.add)
    for _ in range(3):
        worker.run_once()
    assert len(calls) == 2 and calls[0] == calls[1]
    assert calls[1]["open_id"] == "ou_test"
    assert "https://work.example.com/cases/case_test?action=answer" in calls[1]["text"]
    assert "未披露供应商" not in calls[1]["text"]
