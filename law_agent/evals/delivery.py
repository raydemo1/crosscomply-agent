"""The normalized final delivery every arm produces, so grading is arm-blind."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from law_agent.data.schemas import StrictModel

DeliveryStatus = Literal["succeeded", "failed", "blocked", "unanswered_gate"]
JudgmentForm = Literal["definite", "conditional", "abstain"]


class CitedSource(StrictModel):
    """One legal basis the delivery relies on.

    ``source_id`` is filled directly by CrossComply and resolved from the
    title by ``grading.resolve_citations`` for free-text arms.
    """

    title: str
    article_no: str | None = None
    source_id: str | None = None
    can_cite_clause: bool | None = Field(
        default=None, description="CrossComply 的引用门禁结果；其他系统为 None",
    )


class AnswerCitation(StrictModel):
    law_title: str = Field(description="法律文件全称，不加书名号")
    article_no: str | None = Field(default=None, description="条号，如 第五条；无法定位到条时为 null")


class FreeTextAnswer(StrictModel):
    """Output contract for arms that answer in one structured message."""

    judgment_form: JudgmentForm = Field(description="definite 为确定结论；conditional 为附条件分支；abstain 为证据不足不下结论")
    legal_path: str | None = Field(description="适用的出境路径，如 安全评估/标准合同/认证/免予；不确定时为 null")
    conclusion: str = Field(description="完整结论与理由")
    citations: list[AnswerCitation] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    questions_for_applicant: list[str] = Field(default_factory=list)


class Delivery(StrictModel):
    arm: str
    scenario_id: str
    status: DeliveryStatus
    judgment_form: JudgmentForm | None = None
    risk_level: str | None = None
    legal_path: str | None = None
    conclusion: str = ""
    citations: list[CitedSource] = Field(default_factory=list)
    missing_information: list[str] = Field(default_factory=list)
    questions_asked: list[str] = Field(default_factory=list)
    answers_used: list[str] = Field(default_factory=list)
    freshness_hold: bool | None = None
    counters: dict[str, int] = Field(default_factory=dict)
    usage: list[dict[str, Any]] = Field(default_factory=list)
    latency_ms: int = 0
    error: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict, description="arm 原始交付，供 judge 与人工复核")


def from_free_text(arm: str, scenario_id: str, answer: FreeTextAnswer, **extra: Any) -> Delivery:
    return Delivery(
        arm=arm, scenario_id=scenario_id, status="succeeded",
        judgment_form=answer.judgment_form,
        risk_level="insufficient_evidence" if answer.judgment_form == "abstain" else None,
        legal_path=answer.legal_path, conclusion=answer.conclusion,
        citations=[CitedSource(title=c.law_title, article_no=c.article_no) for c in answer.citations],
        missing_information=answer.missing_information,
        questions_asked=answer.questions_for_applicant,
        raw=answer.model_dump(mode="json"), **extra,
    )
