"""Behavior tests for the CrossComply case workbench API."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from law_agent.review.api import ReviewResponse, create_app
from law_agent.review.case_store import InMemoryCaseStore
from law_agent.review.schemas import EvidenceSelfCheck, ReviewFacts, ReviewResult


def _review_response(*, risk_level: str = "medium", missing: list[str] | None = None) -> ReviewResponse:
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
        evidence_self_check=EvidenceSelfCheck(status="sufficient" if risk_level != "insufficient_evidence" else "insufficient"),
    )


@pytest.fixture
def app(tmp_path: Path):
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text("", encoding="utf-8")
    return create_app(
        chunks_path=chunks,
        case_store=InMemoryCaseStore(seed_password="pw"),
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

    submitted = requester.post(
        f"/api/cases/{case_id}/status",
        json={"status": "submitted"},
    )
    assert submitted.status_code == 200
    assert submitted.json()["case"]["facts_confirmed"] is True

    denied = requester.post(
        f"/api/cases/{case_id}/status",
        json={"status": "in_review"},
    )
    assert denied.status_code == 403

    _login(reviewer, "reviewer@crosscomply.local")
    visible = reviewer.get(f"/api/cases/{case_id}")
    assert visible.status_code == 200
    reviewing = reviewer.post(
        f"/api/cases/{case_id}/status",
        json={"status": "in_review"},
    )
    assert reviewing.status_code == 200


def test_run_persists_review_actions_and_audit_events(app, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("law_agent.review.api._run_review", lambda _app, _case: _review_response())
    with TestClient(app) as client:
        _login(client, "reviewer@crosscomply.local")
        case_id = _create_case(client)
        assert client.post(f"/api/cases/{case_id}/status", json={"status": "submitted"}).status_code == 200

        response = client.post(f"/api/cases/{case_id}/run")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["run_status"] == "completed"
        assert body["case"]["risk_level"] == "medium"
        assert len(body["actions"]) == 2
        assert {event["event_type"] for event in body["events"]} >= {
            "case_created", "status_changed", "review_started", "review_completed",
            "action_created",
        }

        persisted = client.get(f"/api/cases/{case_id}").json()
        assert persisted["case"]["response"]["review_result"]["conclusion"]


def test_evidence_gap_stays_in_needs_info(app, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "law_agent.review.api._run_review",
        lambda _app, _case: _review_response(risk_level="insufficient_evidence", missing=["年度数据量"]),
    )
    with TestClient(app) as client:
        _login(client, "reviewer@crosscomply.local")
        case_id = _create_case(client)
        assert client.post(f"/api/cases/{case_id}/status", json={"status": "submitted"}).status_code == 200

        response = client.post(f"/api/cases/{case_id}/run")
        assert response.status_code == 200
        assert response.json()["run_status"] == "needs_info"

        complete = client.post(f"/api/cases/{case_id}/status", json={"status": "completed"})
        assert complete.status_code == 409


def test_actions_feedback_and_dashboard_are_persisted(app) -> None:
    with TestClient(app) as client:
        _login(client, "reviewer@crosscomply.local")
        case_id = _create_case(client)
        action = client.post(
            f"/api/cases/{case_id}/actions",
            json={"title": "补充供应商合同", "priority": "high"},
        )
        assert action.status_code == 200
        action_id = action.json()["id"]
        updated = client.patch(f"/api/actions/{action_id}", json={"status": "completed"})
        assert updated.status_code == 200
        assert updated.json()["status"] == "completed"
        events = client.get(f"/api/cases/{case_id}/events")
        assert events.status_code == 200
        assert any(item["event_type"] == "action_updated" for item in events.json()["items"])

        feedback = client.post(
            f"/api/cases/{case_id}/feedback",
            json={"conclusion_useful": True, "notes": "证据引用清晰"},
        )
        assert feedback.status_code == 200
        assert feedback.json()["conclusion_useful"] is True

        summary = client.get("/api/dashboard/summary")
        assert summary.status_code == 200
        assert summary.json()["total_cases"] == 1
