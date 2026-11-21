"""Scenario bank: the single source of evaluation scenarios.

A scenario carries a frozen review date, a multi-file material bundle, where
its fact pattern came from, and up to two label layers: retrieval labels
(article-level) and an Agent rubric (judgment boundaries). Synthetic
scenarios live in Git under ``bank/data``; field scenarios exported from
reviewer corrections live outside Git (they derive from real cases).
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Literal

from pydantic import Field, field_validator, model_validator

from law_agent.data.schemas import StrictModel

Split = Literal["dev", "test", "field"]

JudgmentPoint = Literal[
    # A 机制选择
    "mechanism.threshold", "mechanism.sensitive", "mechanism.ciio", "mechanism.important_data",
    "mechanism.portfolio",
    # B 免予情形
    "exemption.contract", "exemption.hr", "exemption.emergency", "exemption.foreign_origin",
    "exemption.no_personal_info", "exemption.ftz_negative_list", "exemption.counting",
    # C 程序
    "procedure.assessment", "procedure.standard_contract", "procedure.certification",
    # D 地域 / E 行业
    "region.ftz", "region.gba", "region.shenzhen", "industry.automotive", "industry.financial",
    "industry.miit",
    # F 相关义务
    "duty.notice_consent", "duty.pia", "duty.network_data_regulation", "duty.cybersecurity_review",
    "duty.deidentification",
    # G 时效 / H 边界
    "temporal.version", "boundary.out_of_corpus", "boundary.material_conflict",
    "boundary.missing_fact", "boundary.out_of_scope", "boundary.freshness",
]

NoiseType = Literal[
    "count_unit_mismatch",      # 记录数、账号数与去重自然人数混用
    "approximate_count",        # "约""大概"等不精确数字
    "material_conflict",        # 文件之间互相矛盾
    "missing_fact",             # 决定路径的事实缺失
    "boilerplate",              # 与本次判断无关的大段样板文字
    "business_legal_claim",     # 业务方自行给出的法律结论
    "mixed_population",         # 同一批数据混有不同法律地位的人群或数据流
]


class Provision(StrictModel):
    """One legal source, optionally narrowed to an article."""

    source_id: str
    article_no: str | None = Field(
        default=None, description="条号，如 第五条；None 只允许用于整篇作为参考的不可引用来源",
    )
    note: str = Field(min_length=1, description="为什么需要它，一句话")

    @property
    def key(self) -> str:
        return f"{self.source_id} {self.article_no}" if self.article_no else self.source_id


class ExcludedProvision(Provision):
    reason: Literal["version_superseded", "conflict_rule_overridden", "region_mismatch",
                    "industry_mismatch", "not_applicable"]


class SheetTable(StrictModel):
    name: str
    rows: list[list[str | int | float | None]] = Field(min_length=1)


class MaterialFile(StrictModel):
    """One file of the bundle. ``text`` for documents, ``sheets`` for XLSX."""

    filename: str
    kind: Literal["email", "business_memo", "data_inventory", "contract_excerpt",
                  "privacy_policy", "consent_form", "pia_draft", "internal_policy", "meeting_notes",
                  "case_export"]
    text: str | None = None
    sheets: list[SheetTable] | None = None

    @model_validator(mode="after")
    def one_body(self) -> MaterialFile:
        is_sheet = self.filename.lower().endswith(".xlsx")
        if is_sheet != (self.sheets is not None) or (self.text is None) == (self.sheets is None):
            raise ValueError(f"{self.filename}: .xlsx 只能用 sheets，其他文件只能用 text")
        return self


class FactPatternSource(StrictModel):
    kind: Literal["enforcement", "approved_case", "official_qna", "synthetic", "field_feedback"]
    urls: list[str] = Field(default_factory=list)
    public_facts: str = Field(min_length=1, description="公开来源实际写明的事实；synthetic 时写模拟的常见错误")
    synthesized: str = Field(min_length=1, description="本场景在公开事实之外补充或改写的部分")

    @model_validator(mode="after")
    def urls_for_public_sources(self) -> FactPatternSource:
        if self.kind in {"enforcement", "approved_case", "official_qna"} and not self.urls:
            raise ValueError("基于公开来源的事实模式必须给出 URL")
        return self


class Persona(StrictModel):
    persona_id: str
    name: str = Field(description="虚构主体名称，不得使用真实企业名称")
    profile: str
    region: str
    in_free_trade_zone: bool
    industry: str


class ScriptedAnswer(StrictModel):
    """A pre-authored human answer used when an arm pauses to ask."""

    answer: str = Field(min_length=1)
    provenance: Literal["applicant_statement", "reviewer_instruction"] = "applicant_statement"
    gate_id: str | None = None


class ControlledWebResult(StrictModel):
    url: str
    title: str
    text: str
    published_date: str


class ControlledWebFixture(StrictModel):
    """Hold sources out of the corpus and serve fixed official Web results."""

    held_out_source_ids: list[str] = Field(min_length=1)
    results: list[ControlledWebResult] = Field(min_length=1, max_length=3)


class LabeledQuery(StrictModel):
    """A fixed search query and the labeled provisions it is written to find.

    ``targets`` use ``Provision.key`` (``source_id`` or ``source_id 第X条``). An empty list
    marks a query a careless reviewer would also run — the temptation in a temporal case.
    """

    text: str = Field(min_length=1)
    targets: list[str] = Field(default_factory=list)


class RetrievalLabels(StrictModel):
    queries: list[LabeledQuery] = Field(min_length=1, max_length=4)
    must_cite: list[Provision] = Field(default_factory=list)
    must_retrieve: list[Provision] = Field(default_factory=list)
    optional: list[Provision] = Field(default_factory=list)
    must_not_cite: list[ExcludedProvision] = Field(default_factory=list)

    @model_validator(mode="after")
    def article_level_citations(self) -> RetrievalLabels:
        if any(item.article_no is None for item in self.must_cite):
            raise ValueError("must_cite 必须标到条号")
        if not (self.must_cite or self.must_retrieve):
            raise ValueError("检索标签至少需要一条 must_cite 或 must_retrieve")
        labeled = {p.key for p in (*self.must_cite, *self.must_retrieve, *self.optional)}
        targeted = {t for q in self.queries for t in q.targets}
        if unknown := sorted(targeted - labeled):
            raise ValueError(f"查询目标不在标签中：{unknown}")
        if untargeted := sorted({p.key for p in (*self.must_cite, *self.must_retrieve)} - targeted):
            raise ValueError(f"必引或必检来源没有对准它的查询：{untargeted}")
        return self


class AgentRubric(StrictModel):
    """Judgment boundaries for the final delivery — outcome level, never steps.

    Deterministic fields: ``should_abstain``, ``acceptable_outcomes``,
    ``must_cover_sources``, ``forbidden_clause_sources``, ``expect_freshness_hold``.
    Judge fields: the rest.
    """

    should_abstain: bool | None = False
    acceptable_outcomes: list[str] = Field(default_factory=list)
    must_cover_sources: list[str] = Field(default_factory=list)
    forbidden_clause_sources: list[str] = Field(default_factory=list)
    expect_freshness_hold: bool = False
    must_not_assume: list[str] = Field(default_factory=list)
    required_exceptions: list[str] = Field(default_factory=list)
    clarification_expectations: list[str] = Field(default_factory=list)
    allowed_judgments: list[str] = Field(default_factory=list)
    forbidden_judgments: list[str] = Field(default_factory=list)
    notes: str = ""


class Scenario(StrictModel):
    scenario_id: str
    split: Split
    label_status: Literal["draft", "approved"] = "draft"
    reviewer: str | None = None
    persona_id: str | None = None
    as_of_date: date
    question: str = Field(min_length=1)
    intake: dict[str, object] = Field(default_factory=dict, description="已确认填报，键同 IntakePayload")

    @field_validator("intake")
    @classmethod
    def intake_is_a_valid_payload(cls, value: dict[str, object]) -> dict[str, object]:
        from law_agent.review.http.schemas import IntakePayload

        if unknown := sorted(set(value) - set(IntakePayload.model_fields)):
            raise ValueError(f"intake 含未知字段：{unknown}")
        IntakePayload.model_validate(value)
        return value
    materials: list[MaterialFile] = Field(min_length=1)
    judgment_points: list[JudgmentPoint] = Field(default_factory=list)
    noise_types: list[NoiseType] = Field(default_factory=list)
    fact_pattern_source: FactPatternSource
    selection_reason: str = ""
    reference_basis: list[str] = Field(default_factory=list)
    answer_leak_sources: list[str] = Field(
        default_factory=list, description="语料中直接回答本题的来源（如政策问答），报告时单独统计",
    )
    retrieval: RetrievalLabels | None = None
    agent_rubric: AgentRubric | None = None
    portfolio: dict[str, object] | None = Field(
        default=None, description="冻结的同主体累计台账（PortfolioSummary 序列化），注入方式同生产",
    )
    scripted_answers: list[ScriptedAnswer] = Field(default_factory=list)
    controlled_web: ControlledWebFixture | None = None
    tags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent(self) -> Scenario:
        if self.retrieval is None and self.agent_rubric is None:
            raise ValueError("至少需要检索标签或 Agent rubric 之一")
        if self.label_status == "approved" and not self.reviewer:
            raise ValueError("approved 标签必须记录审定人")
        if self.split in {"dev", "test"} and (not self.persona_id or not self.judgment_points):
            raise ValueError("dev/test 场景必须指定 persona_id 与 judgment_points")
        return self


def scenario_hash(scenario: Scenario) -> str:
    """Content hash that binds review records and the locked test split."""

    payload = scenario.model_dump(mode="json", exclude={"label_status", "reviewer"})
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_materials(scenario: Scenario) -> str:
    """Plain-text view of the bundle for arms that take a single prompt."""

    from law_agent.review.materials import SHEET_HEADING

    parts = []
    for material in scenario.materials:
        if material.sheets is None:
            body = material.text or ""
        else:
            body = "\n".join(
                "\n".join([SHEET_HEADING + json.dumps(table.name, ensure_ascii=False),
                           *(json.dumps(row, ensure_ascii=False) for row in table.rows)])
                for table in material.sheets
            )
        parts.append(f"=== 文件：{material.filename} ===\n{body}")
    return "\n\n".join(parts)
