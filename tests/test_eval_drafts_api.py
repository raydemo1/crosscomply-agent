import json

import pytest

from law_agent.evals.bank.schema import scenario_hash
from law_agent.evals.field import export_ready, load_field_scenarios
from tests.test_work_inbox_transfers import client_for, make_app, signed_case, user_for


@pytest.fixture
def setup(tmp_path):
    app = make_app(tmp_path / "chunks.jsonl")
    owner, reviewer = (user_for(app, role) for role in ("requester", "reviewer"))
    case = signed_case(app, owner, intake={"business_entity": "待脱敏公司", "followup_answers": {"facts": "不导出"}})
    return app, case, client_for(app, owner), client_for(app, reviewer)


def sanitized(**changes):
    return {
        "intake": {}, "question": "测试出境业务的复核边界", "material_text": "示例材料",
        "selection_reason": "检验结论与证据的匹配", "reference_basis": ["适用法源"],
        "rubric": {"allowed_judgments": ["在前提成立时作条件性说明"], "forbidden_judgments": ["不得推断已经完成申报"]},
        "deidentified": True, **changes,
    }


def test_permissions_signals_and_validation(setup):
    _app, case, applicant, reviewer = setup
    source = f"/api/cases/{case['id']}"
    assert applicant.get(f"{source}/eval-signals").status_code == 403
    assert applicant.post(f"{source}/eval-drafts").status_code == 403
    assert applicant.get("/api/eval-drafts").status_code == 403
    assert applicant.get("/api/governance/quality").status_code == 403
    assert reviewer.post(f"{source}/feedback", json={"citation_verdicts": {"chunk_1": "wrong"}}).status_code == 200
    assert reviewer.get(f"{source}/eval-signals").json()["items"] == [{"kind": "citation_wrong", "citation_refs": ["chunk_1"]}]
    created = reviewer.post(f"{source}/eval-drafts")
    assert created.status_code == 201, created.text
    draft = created.json()
    assert "followup_answers" not in draft["intake"]
    assert "original_entities" not in draft and "拟使用新的供应商" in draft["material_text"]
    assert reviewer.post(f"{source}/eval-drafts").json()["detail"]["draft_id"] == draft["id"]
    route = f"/api/eval-drafts/{draft['id']}"
    assert applicant.get(route).status_code == applicant.patch(route, json={}).status_code == 403
    assert applicant.post(f"{route}/ready").status_code == 403
    assert reviewer.post(f"{route}/ready").status_code == 422
    phone = "13800138000"
    patch = sanitized(material_text=f"王经理 {phone} · 待脱敏公司", deidentified=False)
    patch["rubric"]["must_cover_sources"] = ["missing_source"]
    assert reviewer.patch(route, json=patch).status_code == 200
    invalid = reviewer.post(f"{route}/ready")
    assert invalid.status_code == 422 and phone not in invalid.text and "待脱敏公司" not in invalid.text
    errors = invalid.json()["detail"]["errors"]
    assert {"material_text", "deidentified", "rubric.must_cover_sources"} <= {error["field"] for error in errors}
    assert any(error.get("masked") == "*" * 11 for error in errors)
    assert reviewer.patch(route, json=sanitized()).status_code == 200
    assert reviewer.post(f"{route}/ready").json()["status"] == "ready"
    assert reviewer.patch(route, json={"question": "不允许修改"}).status_code == 409
    assert reviewer.post(f"{route}/reopen").json()["deidentified"] is False
    assert reviewer.post(f"{route}/discard").json()["status"] == "discarded"
    assert reviewer.post(f"{source}/eval-drafts").status_code == 201


def test_export_dry_run_and_hash_conflict(setup, tmp_path, monkeypatch):
    app, case, _applicant, reviewer = setup
    created = reviewer.post(f"/api/cases/{case['id']}/eval-drafts").json()
    route = f"/api/eval-drafts/{created['id']}"
    reviewer.patch(route, json=sanitized())
    assert reviewer.post(f"{route}/ready").status_code == 200
    records = app.state.eval_draft_store
    output = tmp_path / "field"
    assert load_field_scenarios(output) == []
    assert export_ready(records, output, set(), dry_run=True) == 1
    assert not output.exists() and records.get(created["id"])["status"] == "ready"
    assert export_ready(records, output, set()) == 1
    scenario, = load_field_scenarios(output)
    assert scenario.split == "field" and scenario.label_status == "draft"
    assert scenario.materials[0].kind == "case_export"
    assert records.get(created["id"])["exported_sha256"] == scenario_hash(scenario)
    assert export_ready(records, output, set()) == 0
    from law_agent.evals.bank.loader import load_scenarios
    from law_agent.evals.review import export_review_package

    assert load_scenarios(field_dir=output, ids=[scenario.scenario_id]) == [scenario]
    assert export_review_package([scenario], tmp_path / "review")["counts"] == {"field": 1}
    path = output / f"{scenario.scenario_id}.json"
    line = json.loads(path.read_text(encoding="utf-8"))
    line["question"] = "被改动的题目"
    original = json.dumps(line, ensure_ascii=False)
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match="已变化"):
        export_ready(records, output, set())
    assert path.read_text(encoding="utf-8") == original


def test_quality_denominators_and_role_filter(setup):
    _app, case, applicant, reviewer = setup
    route = f"/api/cases/{case['id']}/feedback"
    quality = reviewer.get("/api/governance/quality").json()
    assert quality["conclusion_useful_rate"] is None and quality["citation_checks"]["accuracy"] is None
    applicant.post(route, json={"conclusion_useful": False})
    assert reviewer.get("/api/governance/quality").json()["reviewed_cases"] == 0
    reviewer.post(route, json={"conclusion_useful": True, "citation_verdicts": {"a": "wrong", "b": "correct"}})
    quality = reviewer.get("/api/governance/quality").json()
    assert quality["reviewed_cases"] == 1 and quality["conclusion_useful_rate"] == 1
    assert quality["citation_checks"] == {"correct": 1, "wrong": 1, "accuracy": 0.5}
