"""Deterministic review completion and canonical applicant-task contracts."""

import pytest

from law_agent.review.agent import AgentState, run_agent
from law_agent.review.agent_tools import ComplianceAgentTools
from law_agent.review.case_store import InMemoryCaseStore
from law_agent.review.dates import business_today
from law_agent.review.remediation import reconcile_guided_action_list
from law_agent.review.result_builder import (
    LLMReviewResultDraft,
    ReviewIssueDraft,
    ReviewQuestionDraft,
)
from law_agent.review.workflow import review_has_missing_information


def tools():
    result = ComplianceAgentTools.__new__(ComplianceAgentTools)
    result._default_as_of_date = business_today()
    result._chunks = []
    result._chunks_by_id = {}
    result._neighbor_hits = {}
    result._top_k = 10
    result._material_text = ''
    result._material_versions_by_id = {}
    result._semantic_verifier = object()
    return result


def draft(**updates):
    return LLMReviewResultDraft(
        risk_level='insufficient_evidence',
        decision_summary='当前执行预算不足，审查尚未完成，暂时不能形成确定的法律路径与审批结论，需要审核人重试审查。',
        conclusion='审查尚未完成。', **updates)


def test_budget_exhaustion_without_concrete_gaps_is_retryable_not_an_empty_applicant_queue():
    def unexpected(*args, **kwargs):
        pytest.fail('No investigation or verifier is needed to reproduce budget exhaustion')

    finalizer = tools()
    state = run_agent(
        AgentState(goal='审查', max_turns=0), material='', intake={},
        decide=unexpected, search=unexpected, web_search=unexpected,
        finalize=unexpected,
        abstain=lambda report, current: finalizer.finalize(report, current, case_id='case',
            intake_snapshot={'facts': {}}, system_abstention=True),
        checkpoint=lambda _: None)
    plan, blockers, status = reconcile_guided_action_list(
        InMemoryCaseStore(seed_password='pw'), case_id='case', actor_id='reviewer', requester_id='applicant',
        task_result=state.result)
    assert state.result['review_result']['missing_information'] == []
    assert state.result['review_result']['legal_basis_gaps'] == []
    assert plan['tasks'] == blockers == []
    assert review_has_missing_information(state.result)
    assert status == 'run_failed'
    assert state.result['semantic_grounding']['status'] == 'not_run'


def test_each_fact_issue_question_must_be_in_the_canonical_list_before_delivery():
    questions = ['实际累计人数是多少', '两份材料差异的具体原因是什么']
    issues = [ReviewIssueDraft(kind='missing_information', title=q, finding=q,
              material_evidence=[], supporting_chunk_ids=[], question_indexes=[index], recommended_action='确认该事实')
              for index, q in enumerate(questions)]
    with pytest.raises(ValueError, match="question_indexes"):
        tools().finalize(draft(questions=[ReviewQuestionDraft(text=text) for text in questions[:1]], issues=issues), AgentState(goal='审查'),
                         case_id='case', intake_snapshot={'facts': {}}, system_abstention=True)


@pytest.mark.parametrize('payload,expected', [
    ({'review_result': {'risk_level': 'insufficient_evidence', 'missing_information': ['实际累计人数']}}, 'needs_info'),
    ({'review_result': {'risk_level': 'insufficient_evidence', 'legal_basis_gaps': ['尚缺法条正文']}}, 'pending_source_verification'),
    ({'review_result': {'risk_level': 'insufficient_evidence', 'missing_information': ['实际人数'],
                       'legal_basis_gaps': ['尚缺法条正文']}}, 'pending_source_verification'),
    ({'review_result': {'risk_level': 'medium'}, 'semantic_grounding': {'status': 'uncertain'}}, 'run_failed'),
    ({'review_result': {'risk_level': 'medium'}, 'semantic_grounding': {'status': 'not_run'}}, 'run_failed'),
    ({'review_result': {'risk_level': 'medium'}, 'evidence_self_check': {'status': 'insufficient'}}, 'run_failed'),
    ({'review_result': {'risk_level': 'low'}, 'semantic_grounding': {'status': 'supported'}}, 'pending_feishu_approval'),
])
def test_review_routes_actual_fact_source_and_unfinished_work_separately(payload, expected):
    _, _, status = reconcile_guided_action_list(
        InMemoryCaseStore(seed_password='pw'), case_id='case', actor_id='reviewer', requester_id='applicant',
        task_result=payload)
    assert status == expected
    if expected == 'run_failed':
        assert review_has_missing_information(payload)


def test_budget_abstention_keeps_business_questions_from_the_last_draft():
    report = draft(questions=[ReviewQuestionDraft(text='实际累计人数')])
    state = run_agent(AgentState(goal='审查', max_turns=0, report_draft=report), material='', intake={},
        decide=lambda *_: pytest.fail('budget was already exhausted'), search=lambda *_: [], web_search=lambda *_: [],
        finalize=lambda saved, _: {'review_result': saved.model_dump(mode='json')}, checkpoint=lambda _: None)
    assert [q['text'] for q in state.result['review_result']['questions']] == ['实际累计人数']
    assert state.result['review_result']['risk_level'] == 'insufficient_evidence'


def test_complete_canonical_list_creates_only_one_task_per_exact_question():
    questions = ['实际累计人数', '人数差异原因']
    issues = [ReviewIssueDraft(kind='missing_information', title='人数核实', finding='人数仍需确认',
        material_evidence=[], supporting_chunk_ids=[], question_indexes=list(range(len(questions))), recommended_action='确认事实')]
    payload = tools().finalize(draft(questions=[ReviewQuestionDraft(text=text) for text in questions], issues=issues), AgentState(goal='审查'),
                              case_id='case', intake_snapshot={'facts': {}}, system_abstention=True)
    plan, blockers, status = reconcile_guided_action_list(
        InMemoryCaseStore(seed_password='pw'), case_id='case', actor_id='reviewer', requester_id='applicant',
        task_result=payload)
    assert status == 'needs_info'
    assert [task['title'] for task in blockers] == questions
    assert len(plan['tasks']) == 2
