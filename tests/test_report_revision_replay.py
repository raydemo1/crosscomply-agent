"""Replay frozen decisions and verdicts without LLM, search or database services."""

import json

import pytest

from law_agent.review.agent_tools import agent_evidence_self_check
from law_agent.review.semantic_grounding import SemanticVerdict
from law_agent.review.workflow import next_status_after_review, review_has_missing_information
from scripts.analyze_report_revisions import audit_run


def test_self_check_summarizes_sources_without_guessing_paraphrase_equivalence():
    from law_agent.review.result_builder import ReviewQuestionDraft
    from tests.test_review_agent import _draft

    draft = _draft().model_copy(update={"questions": [ReviewQuestionDraft(text="当前实际累计人数") ]})
    verdict = SemanticVerdict(status="uncertain", claim_checks=[], conclusion_reason="事实待确认",
                              missing_facts=["实际累计的自然人数"])
    before = (draft.model_dump(), verdict.model_dump())
    check = agent_evidence_self_check(draft, verdict)
    assert check.status == "insufficient" and len(check.issues) == 2
    assert "报告缺口栏目" in check.issues[0].description
    assert "独立核验记录" in check.issues[1].description
    assert (draft.model_dump(), verdict.model_dump()) == before
    assert not any(fact in reason for fact in draft.missing_information for reason in check.triggered_reasons)


@pytest.mark.parametrize("payload,expected", [
    ({"review_result": {"risk_level": "insufficient_evidence"}}, "run_failed"),
    ({"review_result": {"missing_information": ["来源待核"]}}, "needs_info"),
    ({"semantic_grounding": {"status": "supported", "missing_facts": ["只有核验器发现的事实缺口"]}}, "run_failed"),
    ({"evidence_self_check": {"status": "insufficient"}}, "run_failed"),
    ({"freshness_hold": True}, "pending_source_verification"),
])
def test_declared_blocking_gap_cannot_reach_approval(payload, expected):
    assert next_status_after_review(has_missing_information=review_has_missing_information(payload)) == "needs_info"

    from law_agent.review.enterprise_store import InMemoryEnterpriseStore
    from law_agent.review.remediation import reconcile_guided_action_list
    from law_agent.review.worker import completion_has_missing_information
    from tests.test_review_worker import _queued_task

    class Store:
        def sync_guided_remediation_plan(self, *args, **kwargs):
            return {"tasks": []}

    store = InMemoryEnterpriseStore()
    task = _queued_task(store)
    task.result = payload
    assert completion_has_missing_information(task)
    _, _, status = reconcile_guided_action_list(Store(), case_id="case", actor_id="reviewer",
                                                requester_id=None, task_result=payload)
    assert status == expected


def test_limited_goal_not_blocked_by_optional_advice_or_broader_working_facts():
    payload = {"review_result": {"risk_level": "medium", "missing_information": [],
                                 "recommended_actions": ["可另行核对其他业务的未知人数和目的地"]},
               "review_facts": {"missing_information": ["其他业务人数"]},
               "semantic_grounding": {"status": "supported", "missing_facts": []},
               "evidence_self_check": {"status": "sufficient"}}
    assert next_status_after_review(has_missing_information=review_has_missing_information(payload)) == "pending_feishu_approval"


def test_frozen_audit_detects_missing_context(tmp_path):
    root = tmp_path / "run"
    (root / "case").mkdir(parents=True)
    state = {"turns": 2, "report_draft": None, "evidence": [], "steps": [
        {"number": 1, "action": "read_evidence", "observation": {"read_results": [{"chunk_ids": ["lost"]}]}},
    ]}
    request = {"event": "started", "call_id": 1, "task": "case", "request": {"messages": [
        {"content": "你是企业数据合规执行 Agent"}, {"content": json.dumps({"state": state})},
    ]}}
    response = {"event": "completed", "call_id": 1, "choices": [{"content": json.dumps({"action": "read_material"})}]}
    (root / "manifest.json").write_text(json.dumps({"base_commit": "frozen", "production_cases": [{"case_id": "case", "question": "目标"}]}), encoding="utf-8")
    (root / "model_calls.jsonl").write_text(json.dumps(request) + "\n" + json.dumps(response), encoding="utf-8")
    (root / "case/state.json").write_text(json.dumps({"evidence": [], "steps": state["steps"]}), encoding="utf-8")
    assert audit_run(root)["cases"][0]["continuity"][0]["read_text_complete"] is False
