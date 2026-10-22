"""Explicit product-entry assignments for the existing candidate corpus."""

from law_agent.review.evalset.agent_cases import get_agent_cases
from law_agent.review.evalset.delivery_cases_extra import build_delivery_extensions

QA_CASE_REASONS = {
    "eval_cross_border_008": "回答数量门槛问题，不要求形成案件交付。",
    "eval_standard_contract_003": "解释HR出境条件和准备事项，不要求发起整改。",
    "eval_cross_border_001": "事实不足时先给条件性答复；正式暂停恢复另设控制用例。",
    "eval_shanghai_001": "解释地方清单与适用条件，不自动形成审查案件。",
    "eval_sensitive_002": "回答信息性质问题，不涉及正式审批。",
    "agent_historical_001": "回答指定历史时点的规则问题。",
    "eval_out_of_corpus_001": "验证问答能力边界，不要求正式审查报告。",
    "agent_historical_dedup_001": "回答历史统计口径问题，保留第二轮候选来源。",
    "agent_hr_exception_missing_001": "条件未知时解释HR免予条件，不强制补齐表单。",
    "agent_important_data_001": "回答重要数据与数量的关系。",
    "agent_small_sensitive_001": "回答少量敏感个人信息的门槛问题。",
    "qa_exemption_scope_001": "解释程序免予的边界，不扩大为业务放行或完整审查。",
}

REVIEW_CASE_REASONS = {
    "agent_material_conflict_001": "检验材料冲突和正式事实来源边界。",
    "agent_aligned_intake_001": "检验已确认填报与手续完成状态的分离。",
    "agent_confirmed_material_conflict_001": "检验冻结填报与材料冲突的正式审查协议。",
    "agent_requester_resume_001": "检验申请人补充、暂停恢复与正式确认边界。",
    "agent_reviewer_instruction_001": "检验审查员指令不能升级为案件事实。",
    "agent_freshness_holdout_001": "检验受控法源更新与正式审查放行门禁。",
    "review_customer_transfer_report_001": "正式报告应区分机制判断、手续完成与审批许可。",
    "review_hr_exemption_pending_001": "正式审查的免予证据缺口不得误入审批。",
    "review_internal_policy_authority_001": "内部授权不能替代法定机制或提升为正式法源。",
    "review_material_instruction_injection_001": "材料内的自动化指令不得绕过审查及审批边界。",
}


def delivery_candidates():
    original = get_agent_cases("core")
    return [*original, *build_delivery_extensions(original)]


def classified_cases():
    cases = delivery_candidates()
    assignments = set(QA_CASE_REASONS) | set(REVIEW_CASE_REASONS)
    if set(QA_CASE_REASONS) & set(REVIEW_CASE_REASONS) or assignments != {c.case_id for c in cases}:
        raise ValueError("候选案例的产品入口分类不完整或重复")
    return [{"case_id": c.case_id, "primary_entry": "qa" if c.case_id in QA_CASE_REASONS else "review",
             "reason": (QA_CASE_REASONS | REVIEW_CASE_REASONS)[c.case_id],
             "review_status": c.review_status} for c in cases]


def get_qa_cases():
    classified_cases()
    return [case for case in delivery_candidates() if case.case_id in QA_CASE_REASONS]


def get_review_cases():
    classified_cases()
    return [case for case in delivery_candidates() if case.case_id in REVIEW_CASE_REASONS]
