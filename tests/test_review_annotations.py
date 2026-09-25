from law_agent.review.http.schemas import IntakePayload
import json
from pathlib import Path

from fastapi.testclient import TestClient

from law_agent.review.annotations import FollowupAnalysis, analyze_selection
from law_agent.review.api import create_app
from law_agent.review.case_store import InMemoryCaseStore
from law_agent.review.enterprise_store import InMemoryEnterpriseStore
from law_agent.review.revisions import RevisionDraft


def test_followup_rejects_unverified_citation() -> None:
    class FakeClient:
        def chat_json(self, messages, **_kwargs):
            payload = json.loads(messages[1].content)
            assert payload["selected_quote"] == "OLD"
            return {"finding": "存在风险", "recommendation": "请核实", "citation_refs": ["伪造法源"],
                    "insufficient_evidence": False}

    try:
        analyze_selection(quote="OLD", context="A OLD B", question="有风险吗？",
                          citations=[{"citation_ref": "法源-01"}], client=FakeClient())
    except ValueError as exc:
        assert "未经核实" in str(exc)
    else:
        raise AssertionError("unverified citation accepted")


def test_followup_without_legal_source_is_marked_insufficient() -> None:
    class FakeClient:
        def chat_json(self, messages, **_kwargs):
            return {"finding": "需要核查", "recommendation": "补充法源", "citation_refs": [],
                    "insufficient_evidence": False}

    result = analyze_selection(quote="OLD", context="A OLD B", question="是否合规？",
                               citations=[], client=FakeClient())
    assert result.insufficient_evidence is True


def test_annotation_permissions_span_and_decision(tmp_path: Path, monkeypatch) -> None:
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text("", encoding="utf-8")
    cases = InMemoryCaseStore(seed_password="pw")
    enterprise = InMemoryEnterpriseStore()
    app = create_app(chunks_path=chunks, case_store=cases, enterprise_store=enterprise)
    monkeypatch.setattr("law_agent.review.http.annotations.analyze_selection", lambda **_kwargs:
                        FollowupAnalysis(finding="用途需要核查", recommendation="核对合同用途",
                                         citation_refs=["法源-01"], insufficient_evidence=False))
    monkeypatch.setattr("law_agent.review.http.revisions.generate_revision_draft", lambda **_kwargs:
                        RevisionDraft(proposed_text="更窄的用途", rationale="核对用途", open_points=[]))
    requester = cases.authenticate("requester@crosscomply.local", "pw")
    reviewer = cases.authenticate("reviewer@crosscomply.local", "pw")
    case = cases.create_case(question="审查合同", material_text="AA OLD BB OLD", created_by=requester.id)
    material = enterprise.create_material_version(
        case_id=case["id"], logical_name="合同", filename="contract.txt", content_type="text/plain",
        object_key="test/contract.txt", sha256="a" * 64, byte_size=14, uploaded_by=requester.id,
        parse_status="ready", parsed_text="AA OLD BB OLD",
    )
    snapshot = enterprise.create_material_snapshot(case_id=case["id"], version_ids=[material.id], created_by=reviewer.id)
    rule = enterprise.create_intake_snapshot(case_id=case["id"], material_snapshot_id=snapshot.id, intake=IntakePayload().model_dump(mode="json"), created_by="user_test")
    enterprise.enqueue_review_task(case_id=case["id"], material_snapshot_id=snapshot.id,
                                   intake_snapshot_id=rule.id, model_id="test", data_boundary_summary={})
    cases.update_case(case["id"], response_json={"review_result": {
        "review_result_id": "result_1", "issues": [],
    }, "citation_groups": [{"citations": [{"citation_ref": "法源-01", "title": "测试法",
                                           "can_cite_clause": True, "full_article_text": "仅限必要用途"}]}]})
    path = f"/api/cases/{case['id']}"
    target = {"material_version_id": material.id, "start_offset": 3, "end_offset": 6}
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "requester@crosscomply.local", "password": "pw"}).status_code == 200
        assert client.post(f"{path}/annotations", json={**target, "finding": "人工意见"}).status_code == 403
        assert client.post(f"{path}/annotation-followups", json={**target, "question": "风险？"}).status_code == 403
        assert client.get(f"{path}/annotations").json()["items"] == []
        assert client.post("/api/auth/login", json={"username": "reviewer@crosscomply.local", "password": "pw"}).status_code == 200
        materials = client.get(f"{path}/review-materials").json()["items"]
        assert materials[0]["parsed_text"] == "AA OLD BB OLD"
        assert client.post(f"{path}/annotations", json={**target, "finding": " "}).status_code == 422
        assert client.post(f"{path}/annotations", json={**target, "material_version_id": "other",
                                                          "finding": "错误材料"}).status_code == 409
        assert client.post(f"{path}/annotations", json={**target, "finding": "人工意见"}).status_code == 200
        second = client.post(f"{path}/annotations", json={**target, "start_offset": 10, "end_offset": 13,
                                                             "finding": "第二处相同文字"})
        assert second.status_code == 200, second.text
        assert second.json()["quote"] == "OLD"
        followup = client.post(f"{path}/annotation-followups", json={**target, "question": "风险？"})
        assert followup.status_code == 200, followup.text
        pending = followup.json()
        assert pending["status"] == "pending"
        assert client.post("/api/auth/login", json={"username": "requester@crosscomply.local", "password": "pw"}).status_code == 200
        assert len(client.get(f"{path}/annotations").json()["items"]) == 2
        assert client.post(f"/api/annotations/{pending['id']}/decision", json={"decision": "confirmed", "expected_version": 1}).status_code == 403
        assert client.post("/api/auth/login", json={"username": "reviewer@crosscomply.local", "password": "pw"}).status_code == 200
        assert client.post(f"/api/annotations/{pending['id']}/decision", json={"decision": "confirmed", "expected_version": 2}).status_code == 409
        confirmed = client.post(f"/api/annotations/{pending['id']}/decision", json={"decision": "confirmed", "expected_version": 1})
        assert confirmed.status_code == 200, confirmed.text
        proposal = client.post(f"{path}/issues/{pending['id']}/revision-proposals", json=target)
        assert proposal.status_code == 200, proposal.text
        assert proposal.json()["issue_id"] == pending["id"]
        assert cases.get_case(case["id"])["response"]["review_result"]["review_result_id"] == "result_1"
        assert client.post("/api/auth/login", json={"username": "requester@crosscomply.local", "password": "pw"}).status_code == 200
        visible = client.get(f"{path}/annotations").json()["items"]
        assert len(visible) == 3
        assert all(item["status"] == "confirmed" for item in visible)
        assert client.post("/api/auth/login", json={"username": "reviewer@crosscomply.local", "password": "pw"}).status_code == 200
        later = client.post(f"{path}/annotation-followups", json={**target, "question": "再核查？"}).json()
        rejected = client.post(f"/api/annotations/{later['id']}/decision", json={
            "decision": "rejected", "expected_version": 1,
        })
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["status"] == "rejected"
        assert any(event["event_type"] == "annotation_rejected" for event in cases.list_events(case["id"]))
        later = client.post(f"{path}/annotation-followups", json={**target, "question": "再核查？"}).json()
        cases.update_case(case["id"], response_json={"review_result": {"review_result_id": "result_2", "issues": []}})
        assert client.post(f"/api/annotations/{later['id']}/decision", json={
            "decision": "confirmed", "expected_version": 1,
        }).status_code == 409
        assert client.get(f"{path}/annotations").json()["items"] == []
