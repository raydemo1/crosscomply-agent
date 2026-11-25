"""Structured review report contract shared by the Agent and its gates.

Defines the report draft the Agent submits, the validators the citation gate
applies (grounded claims, legal-basis gaps, business questions, summaries),
citation reference attachment and citation marker injection. The report is
produced by the Agent itself; there is no rule-based fallback path.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from html import escape
from itertools import pairwise
from typing import Annotated, Literal

from pydantic import Field, model_validator

from law_agent.data.schemas import StrictModel
from law_agent.review.report_markdown import REPORT_WRITING_GUIDANCE, report_markdown
from law_agent.review.schemas import (
    LEGAL_BASIS_GAP_GUIDANCE,
    CitationGroup,
    GroundedClaim,
    GroundedClaimDraft,
    IssueKind,
    RetrievalHit,
    RiskLevel,
)
from law_agent.review.web_research import canonical_url


class MaterialEvidenceDraft(StrictModel):
    """Material excerpt proposed by the model.

    The model only knows which frozen material it read and what text it saw;
    ``filename``/``version_number``/offsets are resolved by the finalizer.
    """

    material_version_id: str = Field(description="本轮材料头部或frozen_material_version_ids中的版本编号，不是事实快照ID")
    quote: str = Field(description="该材料版本中逐字存在的原文；正式审查引用须在该版本内唯一出现")


class ReviewQuestionDraft(StrictModel):
    text: str = Field(min_length=1, description="尚未确认且影响当前审查的具体业务问题；同一问题只登记一次，不包含法律依据缺口")
    answer_type: Literal["choice", "count", "text"] | None = Field(default=None, description="该问题适合的回答方式")


class WebImpactDraft(StrictModel):
    url: str = Field(min_length=1, description="从本轮eligible_material_web_urls中选择，不提升其引用资格")
    impact: Literal["supplement", "execution_detail", "core"] = Field(description="补充说明、办理细节或影响核心结论；core须保留证据不足且无确定法律路径")


class ReviewIssueDraft(StrictModel):
    """Issue proposed by the model before deterministic grounding."""

    kind: IssueKind = Field(description=(
        "material_conflict 用于两段不同的冻结材料原文之间的矛盾，须引用冲突双方；"
        "填报、人工陈述与材料不一致且尚待核实时，用 missing_information 描述待确认事项，"
        "双方来源保留在 fact_ledger 和 finding 中，不把事实快照当成材料原文。"
        "legal_gap 表示材料事实和正式法条已共同支持的不合规行为、业务状态或方案缺口，"
        "会进入整改流程；不是待分析的法律问题或假设分支。决定缺口是否成立的事实尚未确认时，"
        "用 missing_information，条件性法律后果放在 finding 或正文中。"
    ))
    title: str
    finding: str
    material_evidence: list[MaterialEvidenceDraft] = Field(default_factory=list, description="本轮冻结材料的原文摘录；legal_gap须有材料事实，事实快照不能当作材料")
    supporting_chunk_ids: list[str] = Field(default_factory=list, description="实际取得且can_cite_clause=true的正式法条编号；legal_gap须同时有材料事实和法条支持")
    question_indexes: list[Annotated[int, Field(strict=True, ge=0)]] = Field(default_factory=list, description="引用本报告questions的从0开始的下标；missing_information和material_conflict类型必须关联至少一个问题，不重复填写问题原文")
    recommended_action: str = Field(description="针对该问题的具体建议，区分确认信息、核实与整改，不将准备措施写成已确定的法定义务")

    def resolve_questions(self, questions: Sequence[ReviewQuestionDraft]) -> list[ReviewQuestionDraft]:
        indexes = self.question_indexes
        if self.kind in {"material_conflict", "missing_information"} and not indexes:
            raise ValueError(f"待确认问题「{self.title}」须通过question_indexes关联questions")
        if len(indexes) != len(set(indexes)) or any(type(i) is not int or not 0 <= i < len(questions) for i in indexes):
            raise ValueError(f"问题项「{self.title}」的question_indexes重复或超出questions范围")
        return [questions[i] for i in indexes]


class LLMReviewResultDraft(StrictModel):
    """正式审查报告。字段联动约定：

    risk_level为insufficient_evidence时legal_path必须为null；legal_basis_gaps非空时，
    risk_level必须为insufficient_evidence且legal_path为null。
    业务待确认问题及回答类型只在questions填写一次，issues通过question_indexes关联。
    法律依据缺口单独登记legal_basis_gaps；网页来源及其影响在web_impacts中成对登记。
    """

    risk_level: RiskLevel = Field(description="针对本次审查目标的风险判断；关键事实或法律依据不足而无法判断时用insufficient_evidence，不以medium代替未知")
    decision_summary: str = Field(min_length=40, max_length=240, description="直接概括本次判断及决定性的前提或限制，不补造人数、规模或持续时间")
    legal_path: str | None = Field(default=None, description="仅填写事实和法源已支持的个案确定路径；条件分支流程写正文，此字段留空")
    conclusion: str = Field(description=REPORT_WRITING_GUIDANCE)
    claims: list[GroundedClaimDraft] = Field(default_factory=list, description="正文法律判断对应的可追溯主张，仅引用本轮实际返回且can_cite_clause=true的chunk_id；解释、清单、标准内容可在正文准确说明，不升级为法律claims")
    reference_chunk_ids: list[str] = Field(default_factory=list, description="报告实际使用的解释、指南、标准等参考原文chunk_id；不升级为正式法律主张；未使用的调查资料不登记")
    trigger_reasons: list[str] = Field(default_factory=list, description="有事实及法源支持的决定性判断要点，通常2至4项，每项简短且保留必要条件；不添加未经确认的情节，没有则留空，不重复全文")
    questions: list[ReviewQuestionDraft] = Field(default_factory=list, description="完整业务待确认清单，包含材料冲突所需问题；问题项通过下标引用，事实确认任务仅由此清单生成")
    legal_basis_gaps: list[str] = Field(default_factory=list, description=LEGAL_BASIS_GAP_GUIDANCE + "由Agent补查或审查人员核实，不生成申请人事实确认或整改任务")
    recommended_actions: list[str] = Field(default_factory=list, description="与当前问题有关的必要下一步；一般建议放此处，不制造issues，没有相关建议可留空")
    risk_boundaries: list[str] = Field(default_factory=list, description="影响当前结论的适用范围或假设变化；只改变明示事实，没有相关边界可留空，不补写完整路径树")
    issues: list[ReviewIssueDraft] = Field(default_factory=list, description="仅记录证据支持的重要问题，区分材料冲突、法律问题与待确认事实；一般准备建议不自动成为法律问题，可留空")
    web_impacts: list[WebImpactDraft] = Field(default_factory=list, max_length=2, description="本轮新官方来源或有更新迹象的已入库来源及其影响；从eligible_material_web_urls选择，URL不重复。没有相关影响则留空，已入库且无更新来源仍用原有法源引用")

    @property
    def missing_information(self) -> list[str]:
        return [question.text.strip() for question in self.questions]

    @property
    def missing_answer_types(self) -> dict[str, Literal["choice", "count", "text"]]:
        return {q.text.strip(): q.answer_type for q in self.questions if q.answer_type is not None}

    @property
    def web_impact(self) -> Literal["none", "supplement", "execution_detail", "core"]:
        return next((impact for impact in ("core", "execution_detail", "supplement")
                     if any(item.impact == impact for item in self.web_impacts)), "none")

    @property
    def material_web_urls(self) -> list[str]:
        return [item.url for item in self.web_impacts]

    @model_validator(mode="after")
    def claims_required_unless_abstaining(self) -> LLMReviewResultDraft:
        if self.risk_level != "insufficient_evidence" and not self.claims:
            raise ValueError("non-abstention result requires grounded claims")
        validate_business_questions(self)
        urls = [canonical_url(item.url) for item in self.web_impacts]
        if len(urls) != len(set(urls)):
            raise ValueError("web_impacts不得重复登记同一URL")
        return self



def validate_legal_basis_gaps(draft: LLMReviewResultDraft) -> None:
    if draft.legal_basis_gaps and (draft.risk_level != "insufficient_evidence" or draft.legal_path is not None):
        raise ValueError("阻塞当前目标的法律依据尚有缺口，应保留证据不足且不填写确定法律路径")


def validate_business_questions(draft: LLMReviewResultDraft) -> None:
    questions = draft.missing_information
    if any(not question for question in questions) or len(questions) != len(set(questions)):
        raise ValueError("questions须为非空且不重复的业务问题")
    if set(questions) & {gap.strip() for gap in draft.legal_basis_gaps}:
        raise ValueError("法律依据缺口不能同时登记为questions业务问题")
    for issue in draft.issues:
        issue.resolve_questions(draft.questions)


def validate_grounded_claims(
    claims: Sequence[GroundedClaimDraft],
    evidence_hits: list[RetrievalHit],
) -> list[GroundedClaim]:
    """Reject references that are absent from this run or lack clause authority."""

    allowed_ids = {hit.chunk_id for hit in evidence_hits}
    citable_ids = {hit.chunk_id for hit in evidence_hits if hit.can_cite_clause}
    for claim in claims:
        unknown = set(claim.supporting_chunk_ids) - allowed_ids
        uncitable = set(claim.supporting_chunk_ids) - citable_ids
        if unknown:
            raise ValueError(f"法律主张引用了未检索到的法条：{sorted(unknown)}")
        if uncitable:
            raise ValueError(
                f"法律主张引用了不可作为正式法条的材料：{sorted(uncitable)}。"
                f"对应 claim 文本：{claim.text[:240]}。"
                "请删除这条 claim，或移除其中的不可引用 chunk；可在 conclusion 中准确说明"
                "参考性材料的文本内容与待核条件，但不要把它放进 claims。正式法律 claims"
                "只引用 can_cite_clause=true 的法条。"
            )
        if not claim.supporting_chunk_ids:
            raise ValueError("法律主张必须引用至少一条正式法条")
    return [GroundedClaim(text=claim.text, supporting_chunk_ids=claim.supporting_chunk_ids) for claim in claims]


def attach_citation_refs(
    claims: list[GroundedClaim],
    citation_groups: list[CitationGroup],
) -> list[GroundedClaim]:
    """Expose stable case-local citation refs while retaining chunk ids internally."""

    refs_by_chunk_id = {
        citation.chunk_id: citation.citation_ref
        for group in citation_groups
        for citation in group.citations
        if citation.citation_ref
    }
    return [
        claim.model_copy(
            update={
                "supporting_citation_refs": [
                    refs_by_chunk_id[chunk_id]
                    for chunk_id in claim.supporting_chunk_ids
                    if chunk_id in refs_by_chunk_id
                ]
            }
        )
        for claim in claims
    ]


# ---------------------------------------------------------------------------
# Markdown sanitization for LLM-generated text fields
# ---------------------------------------------------------------------------

# Drop fenced code block fences (``` or ```lang) — LLM occasionally wraps examples.
_CODE_FENCE_RE = re.compile(r"```[^\n]*\n?")
# Downgrade level-1/2 headings to ### so they don't clash with the page's h1/h2.
_HEADING_DOWNGRADE_RE = re.compile(r"^(#{1,2})(?!#)\s", re.MULTILINE)
# Detect **bold** spans. Non-greedy, disallow nested * to keep it simple.
_BOLD_SPAN_RE = re.compile(r"\*\*([^\*\n]{1,120}?)\*\*")
_SUMMARY_FACT_TOKEN_RE = re.compile(
    r"《[^》\n]{1,80}》|\d+(?:\.\d+)?\s*(?:万)?人|\d+(?:\.\d+)?\s*%"
)



def validate_plain_text_summary(summary: str) -> str:
    value = summary.strip()
    if re.search(r"[\n\r#*`]|(?<!\w)(_+)(?=\S)(.+?)(?<=\S)\1(?!\w)", value):
        raise ValueError("decision_summary must be one plain-text paragraph")
    return value


def validate_decision_summary(summary: str, *, supported_text: str) -> str:
    """Keep the approval summary plain and bounded by reviewed material/evidence."""

    value = validate_plain_text_summary(summary)
    normalized_support = re.sub(r"\s+", "", supported_text)
    unsupported_tokens = [
        token
        for token in _SUMMARY_FACT_TOKEN_RE.findall(value)
        if re.sub(r"\s+", "", token) not in normalized_support
    ]
    if unsupported_tokens:
        raise ValueError(
            f"decision_summary introduced unsupported legal or numeric facts: {unsupported_tokens}"
        )
    return value


# ---------------------------------------------------------------------------
# Inline citation markers: inject ①②③ into the report text so the
# frontend can render clickable superscripts that link each claim to its
# supporting legal article shown in the citation cards below.
# ---------------------------------------------------------------------------

_CIRCLED_NUMBERS = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"

def _compact_markdown_text(value: str) -> str:
    """Normalize inline markdown so a claim can be located in its report."""

    without_tags = re.sub(r"<[^>]+>", "", value or "")
    without_formatting = re.sub(r"[*_`~]", "", without_tags)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", without_formatting, flags=re.UNICODE)


def _report_paragraph_ranges(report: str) -> list[tuple[int, int]]:
    """Locate prose, list items and body cells without altering source offsets."""

    lines = report.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    ranges: list[tuple[int, int]] = []
    cell_indexes: dict[int, int] = {}
    tokens = report_markdown().parse(report)
    for index, token in enumerate(tokens):
        if token.type != "inline" or token.map is None:
            continue
        parent = tokens[index - 1].type if index else ""
        start_line, end_line = token.map
        if parent == "td_open":
            line = lines[start_line].rstrip("\r\n")
            pipes = []
            slashes = 0
            for position, char in enumerate(line):
                if char == "|" and slashes % 2 == 0:
                    pipes.append(position)
                slashes = slashes + 1 if char == "\\" else 0
            edges = [-1, *pipes, len(line)]
            cells = [(left + 1, right) for left, right in pairwise(edges)]
            if pipes and not line[:pipes[0]].strip():
                cells = cells[1:]
            if pipes and not line[pipes[-1] + 1:].strip():
                cells = cells[:-1]
            cell_index = cell_indexes.get(start_line, 0)
            cell_indexes[start_line] = cell_index + 1
            if cell_index >= len(cells):
                continue
            start, end = cells[cell_index]
            start += len(line[start:end]) - len(line[start:end].lstrip())
            end = start + len(line[start:end].rstrip())
            if end > start:
                ranges.append((offsets[start_line] + start, offsets[start_line] + end))
        elif parent == "paragraph_open":
            start, end = offsets[start_line], offsets[end_line]
            source = report[start:end]
            start += len(source) - len(source.lstrip())
            end = start + len(report[start:end].rstrip())
            if end > start:
                ranges.append((start, end))
    return ranges


def _longest_common_substring_len(a: str, b: str) -> int:
    """Length of the longest common substring (dynamic programming, O(len(a)*len(b)))."""

    if not a or not b:
        return 0
    best = 0
    previous = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        current = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                current[j] = previous[j - 1] + 1
                best = max(best, current[j])
        previous = current
    return best


def inject_citation_markers(
    report: str,
    claims: list[GroundedClaim],
    citation_ref_by_chunk_id: dict[str, str],
) -> str:
    """Link uniquely located claim paragraphs to their program-owned sources."""

    paragraphs = _report_paragraph_ranges(report)
    compact_paragraphs = [_compact_markdown_text(report[start:end]) for start, end in paragraphs]
    source_numbers = {ref: i + 1 for i, ref in enumerate(dict.fromkeys(citation_ref_by_chunk_id.values()))}
    insertions: dict[int, list[tuple[int, str]]] = {}
    for index, claim in enumerate(claims):
        text = _compact_markdown_text(claim.text)
        if len(text) < 8:
            continue
        # 结论是 claim 的改写叙述而非原文复述：整句包含几乎必然落空（实测最长公共
        # 子串仅约 25 字）。改为最长公共子串 >= 20 个压缩字符即认定支持，仍要求
        # 只在唯一段落命中，避免把编号挂到无关段落。
        matches = [
            end
            for (start, end), paragraph in zip(paragraphs, compact_paragraphs)
            if text in paragraph or _longest_common_substring_len(text, paragraph) >= 20
        ]
        if len(matches) != 1:
            continue
        refs = insertions.setdefault(matches[0], [])
        existing = {ref for _, ref in refs}
        for chunk_id in claim.supporting_chunk_ids:
            ref = citation_ref_by_chunk_id.get(chunk_id)
            if ref and ref not in existing:
                refs.append((index, ref))
                existing.add(ref)
    for position, refs in sorted(insertions.items(), reverse=True):
        markers = []
        for index, ref in refs:
            number = source_numbers[ref]
            label = _CIRCLED_NUMBERS[number - 1] if number <= len(_CIRCLED_NUMBERS) else str(number)
            markers.append(
                f'<sup class="cite-marker" id="cite-marker-{index}-{number}"'
                f' data-claim-index="{index}" data-citation-ref="{escape(ref, quote=True)}">{label}</sup>'
            )
        report = report[:position] + "".join(markers) + report[position:]
    return report


def report_evidence(
    evidence_hits: list[RetrievalHit], *, claims: list[GroundedClaim], reference_chunk_ids: list[str],
    additional_chunk_ids: Sequence[str] = (),
) -> list[RetrievalHit]:
    """Select declared report sources without discarding the investigation set."""

    used = {*reference_chunk_ids, *additional_chunk_ids,
            *(chunk_id for claim in claims for chunk_id in claim.supporting_chunk_ids)}
    unknown = used - {hit.chunk_id for hit in evidence_hits}
    if unknown:
        raise ValueError(f"报告引用了本轮未取得的资料：{sorted(unknown)}")
    return [hit for hit in evidence_hits if hit.chunk_id in used]
