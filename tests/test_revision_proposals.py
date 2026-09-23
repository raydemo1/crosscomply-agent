import json
from pathlib import Path

from fastapi.testclient import TestClient

from law_agent.review.api import create_app
from law_agent.review.case_store import InMemoryCaseStore
from law_agent.review.enterprise_store import InMemoryEnterpriseStore
from law_agent.review.revisions import (
    InMemoryRevisionStore,
    RevisionConflict,
    RevisionDraft,
    generate_revision_draft,
    locate_target,
    sha256,
)


def test_revision_model_receives_verified_law_and_rejection_feedback() -> None:
    class FakeClient:
        def chat_json(self, messages, **_kwargs):
            assert "不自行补造具体法定义务" in messages[0].content
            context = json.loads(messages[1].content)
            assert context["verified_legal_sources"][0]["full_article_text"] == "仅限必要用途"
            assert context["prior_rejection_feedback"] == ["不得增加无限期保留义务"]
            return {"proposed_text": "NEW", "rationale": "收紧用途", "open_points": []}

    draft = generate_revision_draft(
        issue={"id": "issue"}, target_quote="OLD", base_text="A OLD B",
        citations=[{"full_article_text": "仅限必要用途"}],
        prior_feedback=["不得增加无限期保留义务"], client=FakeClient(),
    )
    assert draft.proposed_text == "NEW"


def test_exact_target_must_be_unique() -> None:
    assert locate_target("ABC OLD XYZ", "OLD") == (4, 7)
    for text in ("ABC XYZ", "OLD and OLD"):
        try:
            locate_target(text, "OLD")
        except RevisionConflict:
            pass
        else:
            raise AssertionError("non-unique target accepted")


def test_accepting_one_proposal_supersedes_other_pending_proposals() -> None:
    revisions = InMemoryRevisionStore()
    base = "A OLD B OTHER C"
    common = {
        "case_id": "case", "source_review_result_id": "review", "source_material_version_id": "material",
        "base_version": 0, "base_sha256": sha256(base), "rationale": "test",
        "open_points_json": [], "citation_refs_json": [], "created_by": "reviewer",
    }
    first = revisions.create({**common, "issue_id": "one", "target_quote": "OLD", "target_start": 2, "target_end": 5, "proposed_text": "NEW"})
    second = revisions.create({**common, "issue_id": "two", "target_quote": "OTHER", "target_start": 8, "target_end": 13, "proposed_text": "DIFFERENT"})
    accepted = revisions.decide(first["id"], decision="accepted", expected_version=1,
                                current_review_result_id="review", current_base_text=base,
                                replacement=None, note=None, actor_id="reviewer")
    assert accepted["accepted_text"] == "NEW"
    assert revisions.get(second["id"])["status"] == "superseded"


def test_revision_proposal_accepts_only_grounded_span_and_keeps_original(
    tmp_path: Path, monkeypatch,
) -> None:
    chunks = tmp_path / "chunks.jsonl"
    chunks.write_text("", encoding="utf-8")
    cases = InMemoryCaseStore(seed_password="pw")
    enterprise = InMemoryEnterpriseStore()
    app = create_app(chunks_path=chunks, case_store=cases, enterprise_store=enterprise)
    received: list[dict] = []
    def draft_revision(**kwargs):
        received.append(kwargs)
        return RevisionDraft(proposed_text="NEW", rationale="删除不合理用途", open_points=["期限待确认"])
    monkeypatch.setattr(
        "law_agent.review.http.revisions.generate_revision_draft",
        draft_revision,
    )
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "reviewer@crosscomply.local", "password": "pw"}).status_code == 200
        user = cases.authenticate("reviewer@crosscomply.local", "pw")
        case = cases.create_case(question="审查合同", material_text="ABC OLD XYZ", created_by=user.id)
        material = enterprise.create_material_version(
            case_id=case["id"], logical_name="合同", filename="contract.txt", content_type="text/plain",
            object_key="test/contract.txt", sha256="a" * 64, byte_size=11, uploaded_by=user.id,
            parse_status="ready", parsed_text="ABC OLD XYZ",
        )
        snapshot = enterprise.create_material_snapshot(case_id=case["id"], version_ids=[material.id], created_by=user.id)
        rule = enterprise.create_rule_snapshot(
            case_id=case["id"], material_snapshot_id=snapshot.id, ruleset_version="test",
            facts={}, determination={},
        )
        enterprise.enqueue_review_task(
            case_id=case["id"], material_snapshot_id=snapshot.id, rule_snapshot_id=rule.id,
            model_id="test", data_boundary_summary={},
        )
        cases.update_case(case["id"], response_json={"review_result": {
            "review_result_id": "result_1", "citations": [{"citation_ref": "法源-01", "title": "测试法", "full_article_text": "仅限必要用途"}],
            "issues": [{"id": "issue_1", "title": "用途过宽",
                "kind": "legal_gap", "finding": "需要收紧用途", "supporting_citation_refs": ["法源-01"],
                "material_evidence": [{"material_version_id": material.id, "quote": "OLD",
                                       "start_offset": 4, "end_offset": 7}]}],
        }})
        path = f"/api/cases/{case['id']}/issues/issue_1/revision-proposals"
        bad = client.post(path, json={"material_version_id": material.id, "start_offset": 0, "end_offset": 3})
        assert bad.status_code == 422
        created = client.post(path, json={"material_version_id": material.id, "start_offset": 4, "end_offset": 7})
        assert created.status_code == 200, created.text
        proposal = created.json()
        assert proposal["open_points"] == ["期限待确认"]
        assert received[-1]["citations"][0]["full_article_text"] == "仅限必要用途"
        assert client.post(path, json={"material_version_id": material.id, "start_offset": 4, "end_offset": 7}).status_code == 409
        rejected = client.post(f"/api/revision-proposals/{proposal['id']}/decision", json={
            "decision": "rejected", "expected_version": 1, "note": "不得增加无限期保留义务",
        })
        assert rejected.status_code == 200, rejected.text
        created = client.post(path, json={"material_version_id": material.id, "start_offset": 4, "end_offset": 7})
        assert created.status_code == 200, created.text
        assert received[-1]["prior_feedback"] == ["不得增加无限期保留义务"]
        proposal = created.json()
        accepted = client.post(f"/api/revision-proposals/{proposal['id']}/decision", json={
            "decision": "accepted", "expected_version": 1, "replacement": "NEW TEXT",
        })
        assert accepted.status_code == 200, accepted.text
        assert accepted.json()["proposed_text"] == "NEW"
        assert accepted.json()["accepted_text"] == "NEW TEXT"
        assert accepted.json()["result_text"] == "ABC NEW TEXT XYZ"
        assert enterprise.get_material_version(material.id).parsed_text == "ABC OLD XYZ"
        draft = client.get(f"/api/cases/{case['id']}/working-draft", params={"material_version_id": material.id})
        assert draft.json()["text"] == "ABC NEW TEXT XYZ"
        repeated = client.post(f"/api/revision-proposals/{proposal['id']}/decision", json={
            "decision": "rejected", "expected_version": 1,
        })
        assert repeated.status_code == 409
