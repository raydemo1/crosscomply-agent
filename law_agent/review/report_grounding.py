"""Located evidence checks for report advice and hypothetical boundaries."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from law_agent.data.schemas import StrictModel
from law_agent.review.result_builder import LLMReviewResultDraft


class ReportCheck(StrictModel):
    target: str = Field(min_length=1)
    status: Literal["supported", "unsupported", "uncertain"]
    reason: str = Field(min_length=1)
    quote: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    missing_facts: list[str] = Field(default_factory=list)
    missing_basis: str | None = None


class ReportChecks(StrictModel):
    checks: list[ReportCheck]
    conclusion_reason: str


SYSTEM_PROMPT = """你核对的是报告单元在本案是否成立，不是它能否在某句法条中找到相似文字。

## 事实与判断背景
report_background是作者待核对的当前判断，不是正确答案；事实来自冻结填报、材料和来源账本。confirmed填报不等于独立核实，extracted核对材料，unverified不升级、conflicted不选边，人工操作指引不是事实。未记录的账本条目不代表来源不存在。

## 核对个案推导
如果断言假设某项事实发生变化，仅替换明示变化的事实，其余已确认事实保持不变。根据实际原文判断这项变化是否足以产生所声称的法律后果；适用条件、例外和衔接关系需一起成立，不能只摘取触发条件。未知不是否定，缺证明本身不产生未被原文规定的后果。
法条转述与个案适用分别核对；reason简述决定判断的事实前提与原文依据。原文不支持后果用unsupported，依据不足用uncertain，忠实的条件性结论或核实、准备、重新评估建议可supported，不把建议自动读成法定义务。
仅据authorities实际原文；解释资料不创设独立义务，保留来源角色、时点、地域和对象边界。背景与断言矛盾时，必须据原文判断，不盲从背景。

## 输出定位反馈
只核验report_units，每个target返回一条check。非supported的quote必须逐字来自该单元；evidence_ids仅用authorities.chunk_id或fact_ledger.source_ref，事实来源不是法源；missing_facts仅写业务事实，missing_basis写法律依据缺口，不把法律定性交给用户。
只输出schema JSON和简明证据解释，不输出工具标记、额外字段或内部思维链。
"""


def report_groups(draft: LLMReviewResultDraft) -> dict[str, list[dict[str, str]]]:
    advice = [
        {"target": f"recommended_actions:{i}", "text": text}
        for i, text in enumerate(draft.recommended_actions) if text.strip()
    ]
    for i, issue in enumerate(draft.issues):
        for field in ("title", "finding", "recommended_action"):
            text = getattr(issue, field)
            if text.strip():
                advice.append({"target": f"issues:{i}:{field}", "text": text})
        advice.extend(
            {"target": f"issues:{i}:unknowns:{j}", "text": text}
            for j, text in enumerate(issue.unknowns) if text.strip()
        )
    return {
        "advice": advice,
        "boundaries": [
            {"target": f"risk_boundaries:{i}", "text": text}
            for i, text in enumerate(draft.risk_boundaries) if text.strip()
        ],
    }


def validate_report_checks(verdict: ReportChecks, payload: dict) -> ReportChecks:
    units = {unit["target"]: unit["text"] for unit in payload["report_units"]}
    targets = [check.target for check in verdict.checks]
    if len(targets) != len(set(targets)) or set(targets) != set(units):
        raise ValueError("栏目核验未完整覆盖报告单元，或包含重复、虚构目标")
    ids = {item["chunk_id"] for item in payload["authorities"]} | {
        entry["source_ref"] for entry in payload["fact_ledger"]
    }
    for check in verdict.checks:
        if check.status != "supported" and not (check.quote or "").strip():
            raise ValueError("未获支持的报告单元缺少问题原句")
        if check.quote and check.quote not in units[check.target]:
            raise ValueError("问题原句不在对应报告单元中")
        if not set(check.evidence_ids) <= ids:
            raise ValueError("栏目核验使用了未取得的证据编号")
    return verdict
