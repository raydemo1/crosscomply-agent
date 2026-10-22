"""Located evidence checks for report advice and hypothetical boundaries."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from law_agent.data.schemas import StrictModel
from law_agent.review.result_builder import LLMReviewResultDraft


class ReportCheck(StrictModel):
    target: str = Field(min_length=1, description="输入core_targets或report_targets中实际存在的字段定位")
    status: Literal["supported", "unsupported", "uncertain"]
    reason: str = Field(min_length=1)
    quote: str | None = None
    evidence_ids: list[str] = Field(default_factory=list, description="本轮法源chunk_id或事实账本source_ref，不编造编号")
    missing_facts: list[str] = Field(default_factory=list, description="阻塞当前判断的业务事实，不包含法律定性问题")
    missing_basis: str | None = Field(default=None, description="仅填写确实影响当前目标内判断的法律原文、版本或衔接缺口；可删去的范围外判断不登记，不转交申请人确认")


def report_groups(draft: LLMReviewResultDraft) -> dict[str, list[dict[str, str]]]:
    advice = [
        {"target": f"recommended_actions:{i}", "text": text}
        for i, text in enumerate(draft.recommended_actions) if text.strip()
    ]
    for i, issue in enumerate(draft.issues):
        for field in ("kind", "title", "finding", "recommended_action"):
            text = getattr(issue, field)
            if text.strip():
                advice.append({"target": f"issues:{i}:{field}", "text": text})
        advice.extend(
            {"target": f"issues:{i}:question_indexes:{j}", "text": str(index)}
            for j, index in enumerate(issue.question_indexes)
        )
    return {
        "advice": advice,
        "boundaries": [
            {"target": f"risk_boundaries:{i}", "text": text}
            for i, text in enumerate(draft.risk_boundaries) if text.strip()
        ],
    }


def core_report_units(draft: LLMReviewResultDraft) -> dict[str, str]:
    units = {
        "risk_level": draft.risk_level,
        "decision_summary": draft.decision_summary,
        "conclusion": draft.conclusion,
    }
    if draft.legal_path is not None:
        units["legal_path"] = draft.legal_path
    for field in ("trigger_reasons", "legal_basis_gaps"):
        units.update({f"{field}:{index}": text for index, text in enumerate(getattr(draft, field))})
    units.update({f"questions:{i}:text": question.text for i, question in enumerate(draft.questions)})
    return units


def report_units(draft: LLMReviewResultDraft) -> dict[str, str]:
    return {unit["target"]: unit["text"] for group in report_groups(draft).values() for unit in group}
