"""Arm-blind grading: deterministic checks, an eval-only judge, outcome classes.

Legal outcome and execution outcome are reported separately. The headline
safety metric is ``wrong_definite``: a confident conclusion that violates the
rubric — the error that costs a company most.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from types import SimpleNamespace
from typing import Any, Literal

from pydantic import Field, model_validator

from law_agent.data.citation_policy import has_clause_locator, has_legal_effect
from law_agent.data.schemas import SourceRecord, StrictModel
from law_agent.evals.bank.schema import Scenario, render_materials
from law_agent.evals.delivery import CitedSource, Delivery, JudgmentForm

JudgeGrade = Literal["pass", "minor_issue", "fail", "not_applicable"]
LegalOutcome = Literal["correct_definite", "correct_conditional", "correct_abstain",
                       "wrong_definite", "other_legal_failure", "unevaluated"]
ExecutionOutcome = Literal["delivered", "budget_exhausted", "runtime_failure", "blocked", "unanswered_gate"]


# --------------------------------------------------------------------------- citations


def _norm_title(title: str) -> str:
    title = re.sub(r"[《》〈〉\s“”\"'（）()]", "", title or "")
    return title.removeprefix("中华人民共和国")


def resolve_citations(citations: Sequence[CitedSource], sources: Sequence[SourceRecord]) -> list[CitedSource]:
    """Map free-text law titles to corpus source ids; leave unknown ones unresolved."""

    by_title: dict[str, list[SourceRecord]] = {}
    for source in sources:
        by_title.setdefault(_norm_title(source.title), []).append(source)
    resolved = []
    for citation in citations:
        if citation.source_id:
            resolved.append(citation)
            continue
        key = _norm_title(citation.title)
        matches = by_title.get(key) or [
            s for title, group in by_title.items() if key and (key in title or title in key) for s in group
        ]
        match = matches[0] if len({m.source_id for m in matches}) == 1 else None
        resolved.append(citation.model_copy(update={"source_id": match.source_id if match else None}))
    return resolved


def is_clause_citation(citation: CitedSource, sources: dict[str, SourceRecord]) -> bool:
    if citation.can_cite_clause is not None:
        return citation.can_cite_clause
    source = sources.get(citation.source_id or "")
    if source is None:
        return False
    probe = SimpleNamespace(library_kind=source.library_kind, citation_role=source.citation_role,
                            doc_type=source.doc_type)
    return has_legal_effect(probe) and has_clause_locator(citation.article_no)


# --------------------------------------------------------------------------- deterministic


class DeterministicGrade(StrictModel):
    missing_required_sources: list[str] = Field(default_factory=list)
    illegal_clause_citations: list[str] = Field(default_factory=list)
    temporal_violations: list[str] = Field(default_factory=list)
    unresolved_citations: int = 0
    abstain_correct: bool | None = None
    outcome_acceptable: bool | None = None
    freshness_hold_correct: bool | None = None
    fail_reasons: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.fail_reasons


def deterministic_grade(scenario: Scenario, delivery: Delivery, sources: dict[str, SourceRecord]) -> DeterministicGrade:
    grade = DeterministicGrade(unresolved_citations=sum(c.source_id is None for c in delivery.citations))
    clause = [c for c in delivery.citations if is_clause_citation(c, sources)]
    clause_sources = {c.source_id for c in clause}
    rubric = scenario.agent_rubric
    if rubric is not None:
        grade.missing_required_sources = [s for s in rubric.must_cover_sources if s not in clause_sources]
        grade.illegal_clause_citations = sorted({c.source_id for c in clause
                                                 if c.source_id in rubric.forbidden_clause_sources})
        if rubric.should_abstain is not None:
            grade.abstain_correct = (delivery.judgment_form == "abstain") == rubric.should_abstain
        if rubric.acceptable_outcomes:
            grade.outcome_acceptable = delivery.risk_level in rubric.acceptable_outcomes
        if delivery.freshness_hold is not None:
            grade.freshness_hold_correct = delivery.freshness_hold == rubric.expect_freshness_hold
    if scenario.retrieval:
        for excluded in scenario.retrieval.must_not_cite:
            if any(c.source_id == excluded.source_id
                   and (excluded.article_no is None or c.article_no == excluded.article_no) for c in clause):
                grade.temporal_violations.append(f"{excluded.source_id} {excluded.article_no or ''}".strip())
    reasons = []
    if grade.missing_required_sources:
        reasons.append("missing_required_sources")
    if grade.illegal_clause_citations:
        reasons.append("illegal_clause_citations")
    if grade.temporal_violations:
        reasons.append("cited_excluded_provision")
    if grade.abstain_correct is False:
        reasons.append("abstention")
    if grade.outcome_acceptable is False:
        reasons.append("outcome_not_acceptable")
    if grade.freshness_hold_correct is False:
        reasons.append("freshness_hold")
    grade.fail_reasons = reasons
    return grade


# --------------------------------------------------------------------------- judge


class JudgeVerdict(StrictModel):
    """Structured eval-only judge result. Never enters the production path."""

    judgment_form: JudgmentForm | None = Field(
        default=None, description="被测交付的结论形态：definite 确定结论；conditional 附条件分支；abstain 不下结论",
    )
    legal_correctness: JudgeGrade = "not_applicable"
    exception_coverage: JudgeGrade = "not_applicable"
    fact_grounding: JudgeGrade = "not_applicable"
    clarification_quality: JudgeGrade = "not_applicable"
    overall_pass: bool | None = None
    rubric_violations: list[str] = Field(
        default_factory=list, description="明确违反禁止判断、缺少必要例外或擅自假设事实的具体内容；轻微表达问题不列入。",
    )
    reasons: list[str] = Field(default_factory=list)
    judge_model: str = ""
    error: str | None = None

    @model_validator(mode="after")
    def consistent_overall(self) -> JudgeVerdict:
        if self.error is not None:
            self.overall_pass = None
        elif self.rubric_violations or "fail" in (
            self.legal_correctness, self.exception_coverage, self.fact_grounding, self.clarification_quality,
        ):
            self.overall_pass = False
        return self


JUDGE_SYSTEM = """你是企业数据出境合规评测员，只评测一个系统的最终交付，不重新做审查，也不知道交付来自哪个系统。
依据：审查问题、审查时点、申请人确认的事实、业务材料、系统的追问记录、最终结论及其法律依据。
先判断交付的结论形态 judgment_form：definite（给出确定的法律路径或义务）、conditional（按未确认事实给出分支）、abstain（证据不足不下结论）。
再按 rubric 逐维判定：
- legal_correctness：法律路径与结论是否正确（门槛、并列条件、路径选择、审查时点有效的规则）。
- exception_coverage：required_exceptions 中的每个豁免或例外是否被独立核对。
- fact_grounding：是否把未经确认的事实当作已确认（对照 must_not_assume），是否正确处理材料之间的矛盾。
- clarification_quality：交付披露的事实缺口与核实建议是否对准 clarification_expectations；无澄清必要时给 not_applicable。
allowed_judgments、forbidden_judgments 是判断边界，不是固定句式，不按字面匹配。
明确违反 forbidden_judgments、遗漏 required_exceptions 或违反 must_not_assume 时，把具体断言与所违反的约束写入 rubric_violations，相应维度标 fail。
minor_issue 只用于不违反上述约束、不改变义务或路径的轻微问题。
确定一个出境机制不等于确定整个业务合规；条件性判断、暂行建议与确定义务要区分；核对摘要、结论与条件说明是否一致。
reference_basis 是出题人的法源核对记录，不代表人工审定；不凭模型记忆扩大法律义务。
reasons 每条一行短评，不输出思维链，只输出 schema JSON。"""


Judge = Callable[[Scenario, Delivery], JudgeVerdict]


def judge_messages(scenario: Scenario, delivery: Delivery) -> list[Any]:
    from law_agent.llm.openai_compatible import ChatMessage

    rubric = scenario.agent_rubric
    shown = {key: value for key, value in delivery.model_dump(mode="json").items()
             if key not in {"arm", "raw", "usage", "counters", "latency_ms"}}
    evidence = [
        {key: chunk.get(key) for key in ("source_id", "article_no", "text")}
        for chunk in (delivery.raw.get("evidence_chunks") or [])
    ][:30]
    lines = [
        f"【审查时点】{scenario.as_of_date.isoformat()}",
        f"【审查问题】{scenario.question}",
        "【确认事实】" + json.dumps(scenario.intake, ensure_ascii=False),
        "【材料】", render_materials(scenario),
        "【Rubric】" + (rubric.model_dump_json() if rubric else "（无）"),
        f"【出题法源核对】{scenario.reference_basis}",
        "【预设人工回答】" + json.dumps(delivery.answers_used, ensure_ascii=False),
        "【最终交付】" + json.dumps(shown, ensure_ascii=False),
        "【交付所引法条原文】" + json.dumps(evidence, ensure_ascii=False),
    ]
    return [ChatMessage(role="system", content=JUDGE_SYSTEM), ChatMessage(role="user", content="\n".join(lines))]


class StructuredJudge:
    def __init__(self, *, model_id: str | None = None, client=None, max_retries: int = 2) -> None:
        from law_agent.config import require_llm_config
        from law_agent.llm.openai_compatible import OpenAICompatibleClient
        from law_agent.review.llm import StructuredLLMNode

        config = require_llm_config()
        self.node = StructuredLLMNode(node_name="evals_judge", output_model=JudgeVerdict,
                                      client=client or OpenAICompatibleClient(config), max_retries=max_retries)
        if model_id:
            self.node.model = model_id
        self.model_id = model_id or config.model

    def __call__(self, scenario: Scenario, delivery: Delivery) -> JudgeVerdict:
        try:
            verdict = self.node.run(judge_messages(scenario, delivery))
            verdict.judge_model = self.model_id
            return verdict
        except Exception as exc:  # noqa: BLE001 - judge failure is not the arm's failure
            return JudgeVerdict(judge_model=self.model_id, error=f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- outcome


class Grade(StrictModel):
    arm: str
    scenario_id: str
    execution: ExecutionOutcome
    legal: LegalOutcome
    deterministic: DeterministicGrade | None = None
    judge: JudgeVerdict | None = None
    failure_categories: list[str] = Field(default_factory=list)


def classify(scenario: Scenario, delivery: Delivery, deterministic: DeterministicGrade | None,
             judge: JudgeVerdict | None) -> Grade:
    if delivery.status != "succeeded":
        execution = {"blocked": "blocked", "unanswered_gate": "unanswered_gate"}.get(delivery.status, "runtime_failure")
        return Grade(arm=delivery.arm, scenario_id=scenario.scenario_id, execution=execution, legal="unevaluated",
                     deterministic=deterministic, judge=judge, failure_categories=[execution])
    execution = "budget_exhausted" if delivery.counters.get("budget_exhausted") else "delivered"
    categories = list(deterministic.fail_reasons) if deterministic else []
    if judge is not None and judge.error is None:
        categories += [name for name in ("legal_correctness", "exception_coverage", "fact_grounding",
                                         "clarification_quality") if getattr(judge, name) == "fail"]
    form = (judge.judgment_form if judge and judge.error is None and judge.judgment_form else None) \
        or delivery.judgment_form
    deterministic_ok = deterministic is None or deterministic.passed
    if judge is None or judge.error is not None or judge.overall_pass is None:
        legal = "other_legal_failure" if not deterministic_ok else "unevaluated"
        if not deterministic_ok and form == "definite":
            legal = "wrong_definite"
    elif deterministic_ok and judge.overall_pass:
        legal = {"definite": "correct_definite", "conditional": "correct_conditional",
                 "abstain": "correct_abstain"}.get(form or "", "correct_conditional")
    else:
        legal = "wrong_definite" if form == "definite" else "other_legal_failure"
    return Grade(arm=delivery.arm, scenario_id=scenario.scenario_id, execution=execution, legal=legal,
                 deterministic=deterministic, judge=judge, failure_categories=sorted(set(categories)))
