"""Behavior tests for the CrossComply case workbench API."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from law_agent.review.api import ReviewResponse, _normalize_review_response_payload, create_app
from law_agent.review.case_store import InMemoryCaseStore
from law_agent.review.enterprise_store import InMemoryEnterpriseStore
from law_agent.review.schemas import EvidenceSelfCheck, ReviewFacts, ReviewResult


def _review_response(
    *, risk_level: str = "medium", missing: list[str] | None = None
) -> ReviewResponse:
    facts = ReviewFacts(
        business_activity="推荐系统",
        data_types=["手机号", "定位信息"],
        sensitive_personal_info=True,
        cross_border_transfer=True,
        overseas_recipient="新加坡云服务商",
        missing_information=missing or [],
    )
    result = ReviewResult(
        review_result_id="result_test",
        review_case_id="engine_case_test",
        trace_id="trace_test",
        risk_level=risk_level,  # type: ignore[arg-type]
        conclusion="当前材料显示该业务需要进一步确认数据出境路径与合规义务。",
        review_facts=facts,
        missing_information=missing or [],
        recommended_actions=["确认境外接收方与合同安排", "补齐年度数据量区间"],
    )
    return ReviewResponse(
        review_case_id="engine_case_test",
        trace_id="trace_test",
        review_facts=facts,
        review_result=result,
        evidence_self_check=EvidenceSelfCheck(
            status="sufficient" if risk_level != "insufficient_evidence" else "insufficient"
        ),
    )


@pytest.fixture
def app(tmp_path: Path):
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text("", encoding="utf-8")
    return create_app(
        chunks_path=chunks,
        case_store=InMemoryCaseStore(seed_password="pw"),
        enterprise_store=InMemoryEnterpriseStore(),
    )


def _login(client: TestClient, username: str) -> None:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "pw"},
    )
    assert response.status_code == 200, response.text


def _create_case(client: TestClient) -> str:
    response = client.post(
        "/api/cases",
        json={
            "question": "这个业务是否需要数据出境安全评估？",
            "material_text": "我们将境内用户手机号和定位信息发送给新加坡云服务商。",
            "intake": {
                "business_activity": "推荐系统",
                "data_types": ["手机号", "定位信息"],
                "cross_border_transfer": True,
                "overseas_recipient": "新加坡云服务商",
            },
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["case"]["id"]


def _freeze_inputs(app, case_id: str, *, needs_info: bool = False) -> None:
    enterprise = app.state.enterprise_store
    version = enterprise.create_material_version(
        case_id=case_id,
        logical_name="vendor_dpa",
        filename="dpa.txt",
        content_type="text/plain",
        object_key=f"cases/{case_id}/dpa.txt",
        sha256="a" * 64,
        byte_size=20,
        uploaded_by="user_test",
        parse_status="ready",
        parsed_text="DPA",
    )
    snapshot = enterprise.create_material_snapshot(
        case_id=case_id, version_ids=[version.id], created_by="user_test"
    )
    enterprise.create_rule_snapshot(
        case_id=case_id,
        material_snapshot_id=snapshot.id,
        ruleset_version="national-cross-border-2024.03-v1",
        facts={},
        determination={
            "status": "needs_info" if needs_info else "determined",
            "needs_info": [{"key": "important_data"}] if needs_info else [],
        },
    )


def test_login_creates_session_and_persists_case(app) -> None:
    with TestClient(app) as client:
        _login(client, "requester@crosscomply.local")
        case_id = _create_case(client)

        detail = client.get(f"/api/cases/{case_id}")
        assert detail.status_code == 200
        assert detail.json()["case"]["status"] == "draft"
        assert detail.json()["case"]["facts_confirmed"] is False
        assert detail.json()["events"][0]["event_type"] == "case_created"

        listing = client.get("/api/cases")
        assert listing.status_code == 200
        assert listing.json()["total"] == 1


def test_role_permissions_and_case_status_flow(app) -> None:
    requester = TestClient(app)
    reviewer = TestClient(app)
    _login(requester, "requester@crosscomply.local")
    case_id = _create_case(requester)
    _freeze_inputs(app, case_id)

    submitted = requester.post(
        f"/api/cases/{case_id}/status",
        json={"status": "pending_review"},
    )
    assert submitted.status_code == 200
    assert submitted.json()["case"]["facts_confirmed"] is True

    denied = requester.post(
        f"/api/cases/{case_id}/status",
        json={"status": "review_running"},
    )
    assert denied.status_code == 403

    _login(reviewer, "reviewer@crosscomply.local")
    visible = reviewer.get(f"/api/cases/{case_id}")
    assert visible.status_code == 200
    system_status = reviewer.post(
        f"/api/cases/{case_id}/status",
        json={"status": "review_running"},
    )
    assert system_status.status_code == 403
    reviewing = reviewer.post(
        f"/api/cases/{case_id}/status",
        json={"status": "needs_info"},
    )
    assert reviewing.status_code == 200


def test_run_persists_queued_task_and_audit_event(app) -> None:
    with TestClient(app) as client:
        _login(client, "reviewer@crosscomply.local")
        case_id = _create_case(client)
        _freeze_inputs(app, case_id)
        assert (
            client.post(
                f"/api/cases/{case_id}/status", json={"status": "pending_review"}
            ).status_code
            == 200
        )

        response = client.post(f"/api/cases/{case_id}/run")
        assert response.status_code == 202, response.text
        body = response.json()
        assert body["status"] == "queued"
        task = client.get(f"/api/tasks/{body['task_id']}")
        assert task.status_code == 200
        assert task.json()["status"] == "queued"
        events = client.get(f"/api/cases/{case_id}/events").json()["items"]
        assert {event["event_type"] for event in events} >= {
            "case_created",
            "status_changed",
            "review_queued",
        }


def test_evidence_gap_stays_in_needs_info(app) -> None:
    with TestClient(app) as client:
        _login(client, "reviewer@crosscomply.local")
        case_id = _create_case(client)
        _freeze_inputs(app, case_id, needs_info=True)
        app.state.case_store.update_case(case_id, status="pending_review")

        response = client.post(f"/api/cases/{case_id}/run")
        assert response.status_code == 409
        assert "关键事实缺失" in response.json()["detail"]

        complete = client.post(f"/api/cases/{case_id}/status", json={"status": "approved"})
        assert complete.status_code == 403


def test_remediation_feedback_and_dashboard_are_persisted(app) -> None:
    with TestClient(app) as client:
        _login(client, "reviewer@crosscomply.local")
        case_id = _create_case(client)
        reviewer_id = client.get("/api/auth/me").json()["user"]["id"]
        plan = client.post(
            f"/api/cases/{case_id}/remediation-plan",
            json={
                "tasks": [{
                    "title": "补充供应商合同",
                    "priority": "high",
                    "assignee_id": reviewer_id,
                    "due_date": "2026-08-30",
                }],
            },
        )
        assert plan.status_code == 200, plan.text
        task_id = plan.json()["tasks"][0]["id"]
        activated = client.post(f"/api/remediation-plans/{plan.json()['id']}/activate")
        assert activated.status_code == 200, activated.text
        updated = client.patch(
            f"/api/remediation-tasks/{task_id}",
            json={"description": "补充并归档已签署的供应商合同"},
        )
        assert updated.status_code == 200
        assert updated.json()["description"] == "补充并归档已签署的供应商合同"
        started = client.post(f"/api/remediation-tasks/{task_id}/start")
        assert started.status_code == 200, started.text
        submission = client.post(
            f"/api/remediation-tasks/{task_id}/submissions",
            json={
                "note": "已补充并归档供应商合同。",
                "evidence": [{
                    "kind": "link",
                    "label": "合同归档记录",
                    "uri": "https://example.com/contracts/vendor-dpa",
                }],
            },
        )
        assert submission.status_code == 200, submission.text
        reviewed = client.post(
            f"/api/remediation-submissions/{submission.json()['id']}/review",
            json={"decision": "accepted"},
        )
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["status"] == "accepted"
        events = client.get(f"/api/cases/{case_id}/events")
        assert events.status_code == 200
        assert any(item["event_type"] == "remediation_task_updated" for item in events.json()["items"])

        feedback = client.post(
            f"/api/cases/{case_id}/feedback",
            json={"conclusion_useful": True, "notes": "证据引用清晰"},
        )
        assert feedback.status_code == 200
        assert feedback.json()["conclusion_useful"] is True

        summary = client.get("/api/dashboard/summary")
        assert summary.status_code == 200
        assert summary.json()["total_cases"] == 1


def test_old_response_normalization_preserves_unknown_metadata_and_unique_refs() -> None:
    response = {
        "citation_groups": [
            {"usage": "legal_basis", "citations": [{"citation_ref": "法源-02", "chunk_id": "c1"}]},
            {"usage": "conditional_basis", "citations": [{"chunk_id": "c2"}]},
            {
                "usage": "policy_explanation",
                "citations": [{"citation_ref": "法源-02", "chunk_id": "c3"}],
            },
        ],
        "review_result": {
            "claims": [{"text": "结论", "supporting_chunk_ids": ["c1", "c2", "c3"]}],
            "conclusion": '<sup class="cite-marker" data-claim-index="0">①</sup>',
        },
    }

    normalized = _normalize_review_response_payload(response)
    citations = [
        citation for group in normalized["citation_groups"] for citation in group["citations"]
    ]
    assert [citation["citation_ref"] for citation in citations] == ["法源-02", "法源-01", "法源-03"]
    assert len({citation["citation_ref"] for citation in citations}) == 3
    assert citations[1]["doc_type"] == "unknown"
    assert citations[1]["authority"] == "unknown"
    assert normalized["review_result"]["claims"][0]["supporting_citation_refs"] == [
        "法源-02",
        "法源-01",
        "法源-03",
    ]
