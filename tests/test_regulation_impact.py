from datetime import timedelta

from law_agent.data.io import write_manifest
from law_agent.kb.admin import KnowledgeBaseAdminService
from law_agent.kb.change_detection import diff_source
from law_agent.kb.service import make_stable_chunks
from law_agent.review.dates import business_today
from law_agent.review.impact import citation_rows
from tests.test_kb import _chunk, _source
from tests.test_work_inbox_transfers import client_for, make_app, review_task, signed_case, user_for


def make_change():
    source = _source().model_copy(update={"instrument_key": "law_001", "effective_date": "2022-09-01"})
    chunk = _chunk("正文内容甲").model_copy(update={"article_no": "第一条", "instrument_key": source.instrument_key})
    before = make_stable_chunks([chunk], source)
    after = make_stable_chunks([chunk.model_copy(update={"text": "正文内容乙"})], source)
    change = diff_source(source, before, source, after)
    return source, before, change


def index_case(impact, case_id, task_id, source, chunks):
    chunk = chunks[0]
    result = {"review_result": {"citations": [{
        "chunk_id": chunk.chunk_id, "source_id": source.source_id, "title": source.title,
        "article_no": chunk.article_no, "effective_date": source.effective_date, "can_cite_clause": True,
    }]}}
    impact.records.index_citations(task_id, list(citation_rows(task_id, case_id, result, {chunk.chunk_id: chunk})))


def test_change_diff_distinguishes_label_and_body():
    source, chunks, change = make_change()
    assert change.change_type == "body_changed"
    assert change.detail["removed_chunk_ids"] == [chunks[0].chunk_id]
    assert change.detail["changed_articles"][0]["article_no"] == "第一条"
    metadata = diff_source(source, chunks, source.model_copy(update={"title": "新标题"}), chunks)
    assert metadata.change_type == "metadata_changed" and metadata.detail["metadata_only"]
    assert diff_source(source, chunks, source, chunks) is None


def test_pending_blocking_flag_permission_and_dismissal(tmp_path):
    app = make_app(tmp_path / "chunks.jsonl")
    impact = app.state.impact
    owner, reviewer = (user_for(app, role) for role in ("requester", "reviewer"))
    applicant, legal = client_for(app, owner), client_for(app, reviewer)
    case = impact.cases.create_case(question="测试法源变更", material_text="材料", created_by=owner.id, owner_id=owner.id)
    task = review_task(app, case, {})
    source, chunks, change = make_change()
    index_case(impact, case["id"], task.id, source, chunks)
    metadata = diff_source(source, chunks, source.model_copy(update={"title": "新标题"}), chunks)
    assert impact.compute(impact.records.record_change(metadata)) == 0
    event = impact.records.record_change(change, created_by=reviewer.id)
    assert impact.compute_result(event)["flagged_cases"] == 1
    assert impact.compute(event) == 1
    flags = applicant.get("/api/impact-flags").json()["items"]
    flag, = flags
    assert flag["reason"] == "cited_text_changed" and flag["severity"] == "blocking"
    assert any(item["kind"] == "regulation_impact" for item in legal.get("/api/work-inbox").json()["items"])
    blocked = legal.post(f"/api/cases/{case['id']}/feishu-approval")
    assert blocked.status_code == 409 and blocked.json()["detail"]["code"] == "regulation_impact"
    assert applicant.post(f"/api/impact-flags/{flag['id']}/dismiss", json={"note": "无影响"}).status_code == 403
    assert legal.post(f"/api/impact-flags/{flag['id']}/dismiss", json={"note": "已人工复核"}).status_code == 200
    assert not legal.get("/api/impact-flags").json()["items"]


def test_upcoming_promotes_and_rereview_uses_current_activity(tmp_path):
    app = make_app(tmp_path / "chunks.jsonl")
    impact = app.state.impact
    owner, reviewer = (user_for(app, role) for role in ("requester", "reviewer"))
    legal = client_for(app, reviewer)
    case = signed_case(app, owner)
    today = business_today()
    source, chunks, _ = make_change()
    index_case(impact, case["id"], impact.enterprise.get_latest_task(case["id"]).id, source, chunks)
    newer = source.model_copy(update={"source_id": "new_version", "effective_date": (today + timedelta(days=3)).isoformat()})
    event = impact.records.record_change(diff_source(None, [], newer, []))
    assert impact.compute(event, today=today) == 1
    flag, = impact.records.flags()
    assert flag["reason"] == "superseded_upcoming"
    assert impact.records.promote(today) == 0
    assert impact.records.promote(today + timedelta(days=3)) == 1
    route = f"/api/impact-flags/{flag['id']}/rereview"
    assert legal.post(route, json={}).json()["detail"]["code"] == "register_transfer_first"
    assert legal.post("/api/transfer-activities", json={
        "case_id": case["id"], "title": "出境业务", "business_entity": "甲公司", "next_review_on": today.isoformat(),
    }).status_code == 201
    response = legal.post(route, json={})
    assert response.status_code == 201, response.text
    linked = response.json()
    assert linked["status"] == "rereview_started" and linked["reason"] == "superseded"
    impact.records.resolve(linked["rereview_case_id"])
    assert impact.records.get_flag(flag["id"])["status"] == "resolved"


def test_failed_compute_is_retryable(tmp_path, monkeypatch):
    impact = make_app(tmp_path / "chunks.jsonl").state.impact
    _, _, change = make_change()
    event = impact.records.record_change(change)

    def fail(*_args):
        raise RuntimeError("temporary failure")

    with monkeypatch.context() as patch:
        patch.setattr(impact.records, "citations", fail)
        assert impact.compute_result(event)["status"] == "failed"
    assert impact.records.get_change(event["id"])["impact_status"] == "failed"
    assert impact.compute_result(event)["flagged_cases"] == 0
    assert impact.records.get_change(event["id"])["impact_status"] == "computed"


def test_publication_stays_successful_when_impact_computation_fails(tmp_path, monkeypatch):
    impact = make_app(tmp_path / "chunks.jsonl").state.impact
    service = KnowledgeBaseAdminService(tmp_path)
    source, _, _ = make_change()

    def publish():
        write_manifest(tmp_path / "source_manifest.csv", [source])
        return {"action": "added"}

    def fail(*_args):
        raise RuntimeError("temporary failure")

    with monkeypatch.context() as patch:
        patch.setattr(impact.records, "citations", fail)
        result = impact.track_publication(service, source.source_id, publish, job_id="job_1")
    assert result["action"] == "added" and result["impact"]["status"] == "failed"
    event = impact.records.get_change(result["impact"]["change_event_id"])
    assert event["job_id"] == "job_1" and event["impact_status"] == "failed"
    assert impact.compute_result(event)["status"] == "computed"
