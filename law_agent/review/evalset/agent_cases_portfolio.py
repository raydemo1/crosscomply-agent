"""Synthetic candidates for cumulative headcount reasoning."""

from dataclasses import asdict

from law_agent.review.evalset.agent_schemas import AgentCase, AgentRubric
from law_agent.review.portfolio import (
    PortfolioContribution,
    PortfolioSummary,
    band_for,
    fingerprint_summary,
)


def build_portfolio_cases() -> list[AgentCase]:
    scenarios = [
        ("cross_band", 60_000, 50_000, False,
         "应说明去重后可能跨档并要求确认去重人数。",
         "仅凭本案宣告免予，或把未去重的 11 万视为已去重的确定人数。"),
        ("near_assessment", 30_000, 980_000, False,
         "应说明单项最高 98 万、简单加总 101 万，去重后可能跨过 100 万档。",
         "只根据本案人数判断，或把未去重的 101 万当成确定进入安全评估档。"),
        ("stale", 60_000, 50_000, True,
         "应指出上年度记录不能作为本年度人数，需要更新。",
         "把过期业务当作零人或认为累计人数已完整。"),
    ]
    cases = []
    for slug, current, other, stale, allowed, forbidden in scenarios:
        total = 0 if stale else other
        band = band_for(total, 0)
        summary = fingerprint_summary(PortfolioSummary(
            "示例公司", "示例公司", 2026, False,
            [PortfolioContribution(
                "transfer_example", "境外工单归档", "case_example", "intake_example", 2025 if stale else 2026,
                other, 0, "current_year_cumulative", True, "stale_year" if stale else "counted",
            )],
            total, total, 0, 0, band, band,
        ))
        cases.append(AgentCase(
            case_id=f"portfolio_{slug}",
            question="结合本案及同主体业务，说明 2026 年累计人数门槛及需要核实的事项。",
            material_text="示例公司拟由境外服务商处理境内客户的姓名和工作邮箱，不涉及豁免情形。",
            intake={
                "business_entity": "示例公司", "cross_border_transfer": True,
                "contains_personal_information": True, "sensitive_personal_info": False,
                "ciio_status": "not_ciio", "important_data_status": "not_important",
                "count_period": "current_year_cumulative",
                "annual_non_sensitive_count": str(current), "annual_sensitive_count": "0",
            },
            portfolio=asdict(summary),
            rubric=AgentRubric(should_abstain=None, allowed_judgments=[allowed], forbidden_judgments=[forbidden]),
            tags=["candidate", "portfolio"],
            selection_reason="检验跨业务人数上下界和不完整记录的表达。",
            reference_basis=["《促进和规范数据跨境流动规定》第五、七、八条"],
        ))
    return cases
