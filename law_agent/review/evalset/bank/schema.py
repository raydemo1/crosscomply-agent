"""Scenario bank shared by the retrieval benchmark and the Production Agent eval.

One scenario carries a frozen review date, a multi-file material bundle, the
legal provisions a correct review relies on, and where its fact pattern came
from. Retrieval labels and Agent rubrics are separate layers on the same
scenario; the Agent rubric reuses ``AgentRubric`` and is optional until the
Agent suite is rebuilt.
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import Field, model_validator

from law_agent.data.schemas import StrictModel
from law_agent.review.evalset.agent_schemas import AgentRubric

JudgmentPoint = Literal[
    # A 机制选择
    "mechanism.threshold", "mechanism.sensitive", "mechanism.ciio", "mechanism.important_data",
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
    "boundary.missing_fact", "boundary.out_of_scope",
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
                  "privacy_policy", "consent_form", "pia_draft", "internal_policy", "meeting_notes"]
    text: str | None = None
    sheets: list[SheetTable] | None = None

    @model_validator(mode="after")
    def one_body(self) -> MaterialFile:
        is_sheet = self.filename.lower().endswith(".xlsx")
        if is_sheet != (self.sheets is not None) or (self.text is None) == (self.sheets is None):
            raise ValueError(f"{self.filename}: .xlsx 只能用 sheets，其他文件只能用 text")
        return self


class FactPatternSource(StrictModel):
    kind: Literal["enforcement", "approved_case", "official_qna", "synthetic"]
    urls: list[str] = Field(default_factory=list)
    public_facts: str = Field(min_length=1, description="公开来源实际写明的事实；synthetic 时写模拟的常见错误")
    synthesized: str = Field(min_length=1, description="本场景在公开事实之外补充的虚构细节")

    @model_validator(mode="after")
    def urls_for_real_sources(self) -> FactPatternSource:
        if self.kind != "synthetic" and not self.urls:
            raise ValueError("非 synthetic 的事实模式必须给出来源 URL")
        return self


class Persona(StrictModel):
    persona_id: str
    name: str = Field(description="虚构主体名称，不得使用真实企业名称")
    profile: str
    region: str
    in_free_trade_zone: bool
    industry: str


class RetrievalLabels(StrictModel):
    queries: list[str] = Field(min_length=1, max_length=4)
    must_cite: list[Provision] = Field(default_factory=list)
    must_retrieve: list[Provision] = Field(default_factory=list)
    optional: list[Provision] = Field(default_factory=list)
    must_not_cite: list[ExcludedProvision] = Field(default_factory=list)

    @model_validator(mode="after")
    def article_level_citations(self) -> RetrievalLabels:
        if any(item.article_no is None for item in self.must_cite):
            raise ValueError("must_cite 必须标到条号")
        return self


class Scenario(StrictModel):
    scenario_id: str
    split: Literal["dev", "test"]
    label_status: Literal["draft", "approved"] = "draft"
    reviewer: str | None = None
    persona_id: str
    as_of_date: date
    question: str
    intake: dict[str, object] = Field(default_factory=dict, description="已确认填报，键同 IntakePayload")
    materials: list[MaterialFile] = Field(min_length=1)
    judgment_points: list[JudgmentPoint] = Field(min_length=1)
    noise_types: list[NoiseType] = Field(default_factory=list)
    fact_pattern_source: FactPatternSource
    answer_leak_sources: list[str] = Field(
        default_factory=list, description="语料中直接回答本题的来源（如政策问答），报告时单独统计",
    )
    retrieval: RetrievalLabels
    agent_rubric: AgentRubric | None = None
    derived_from: str | None = None

    @model_validator(mode="after")
    def approved_has_reviewer(self) -> Scenario:
        if self.label_status == "approved" and not self.reviewer:
            raise ValueError("approved 标签必须记录审定人")
        return self
