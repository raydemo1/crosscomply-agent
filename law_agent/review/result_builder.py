"""LLM-driven structured review result builder.

Generates governed ``ReviewResult`` via DeepSeek with Pydantic strict
validation. Includes schema definitions, prompt builders, evidence
grounding validation, and citation injection.  Risk level, conclusion,
claims, trigger reasons, recommended actions, and risk boundaries are
all produced by the LLM under schema constraints — there is no
rule-based fallback path.
"""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from html import escape
from typing import Annotated, Literal

from pydantic import Field, model_validator

from law_agent.config import require_llm_config
from law_agent.data.schemas import Chunk, StrictModel
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.agent_context import compact_evidence_context
from law_agent.review.citations import group_citations
from law_agent.review.dates import business_today
from law_agent.review.llm import ReviewWorkflowFailed, StructuredLLMNode
from law_agent.review.schemas import (
    LEGAL_BASIS_GAP_GUIDANCE,
    Citation,
    CitationGroup,
    EvidenceSelfCheck,
    GroundedClaim,
    GroundedClaimDraft,
    IssueKind,
    RetrievalHit,
    RetrievalQuery,
    ReviewFacts,
    ReviewResult,
    RiskLevel,
    SourceEvidencePacket,
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
    conclusion: str = Field(description="围绕当前目标的完整中文答复，说明关键事实、规则与适用推导；条件分支可在此说明，未知不得写成否定")
    claims: list[GroundedClaimDraft] = Field(default_factory=list, description="正文法律判断对应的可追溯主张，仅引用本轮实际返回且can_cite_clause=true的chunk_id；解释、清单、标准内容可在正文准确说明，不升级为法律claims")
    reference_chunk_ids: list[str] = Field(default_factory=list, description="报告实际使用的解释、指南、标准等参考原文chunk_id；不升级为正式法律主张；未使用的调查资料不登记")
    trigger_reasons: list[str] = Field(default_factory=list, description="有事实及法源支持的当前风险理由，不添加未经确认的情节")
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
    """Return body ranges for markdown paragraphs, excluding heading-only blocks."""

    ranges: list[tuple[int, int]] = []

    def add_block(start: int, end: int) -> None:
        block = report[start:end]
        left_trimmed = block.lstrip()
        if not left_trimmed.strip():
            return
        content_start = start + (len(block) - len(left_trimmed))
        content_end = start + len(block.rstrip())
        first_line_end = report.find("\n", content_start, content_end)
        first_line = report[content_start : first_line_end if first_line_end >= 0 else content_end]
        if first_line.lstrip().startswith("#"):
            if first_line_end < 0:
                return
            content_start = first_line_end + 1
            content_end = start + len(block.rstrip())
            if not report[content_start:content_end].strip():
                return
        ranges.append((content_start, content_end))

    block_start = 0
    for separator in re.finditer(r"\n\s*\n+", report):
        add_block(block_start, separator.start())
        block_start = separator.end()
    add_block(block_start, len(report))
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
                if current[j] > best:
                    best = current[j]
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



def build_result_generation_messages(
    *,
    facts: ReviewFacts,
    self_check: EvidenceSelfCheck,
    evidence_hits: list[RetrievalHit],
    question: str | None = None,
    material_text: str | None = None,
    retrieval_queries: list[RetrievalQuery] | None = None,
    second_retrieval: dict[str, object] | None = None,
    source_evidence_packets: list[SourceEvidencePacket] | None = None,
) -> list[ChatMessage]:
    """Provide original evidence and a schema, without legal-answer examples."""

    output_model = LLMReviewResultDraft
    evidence_context = compact_evidence_context({"evidence": [hit.model_dump(mode="json") for hit in evidence_hits]})
    today = business_today()
    payload = {
        "current_date": today.isoformat(),
        "review_date": (facts.as_of_date or today).isoformat(),
        "question": question,
        "material_excerpt": material_text or "",
        "review_facts": facts.model_dump(mode="json"),
        "evidence_self_check": self_check.model_dump(mode="json"),
        "retrieval_queries": [query.model_dump(mode="json") for query in (retrieval_queries or [])],
        "second_retrieval": second_retrieval or {},
        "source_evidence_packets": [{
            "source_id": packet.source_id,
            "representative_chunk_id": packet.representative_chunk.chunk_id,
            "supporting_chunk_ids": [hit.chunk_id for hit in packet.supporting_chunks],
            "neighbor_chunk_ids": [hit.chunk_id for hit in packet.neighbor_chunks],
        } for packet in (source_evidence_packets or [])],
        **evidence_context,
        "citable_chunk_ids": [hit.chunk_id for hit in evidence_hits if hit.can_cite_clause],
    }
    system = """你是CrossComply企业数据合规审查结果生成助手。根据当前问题、本案材料和实际取得的原文，返回符合output_schema的JSON报告。
先回答当前审查目标，说明决定性的事实、适用规则和限制。材料、事实投影和模型判断保留来源区别；未知不作为否定，推断不补作事实。
原文与同源evidence_sources合并理解，完整原文和full_article_text中的条件、例外及适用范围一起使用。代表命中、同源支持和相邻内容的来源身份相同，不因检索位置而改变法律效力。依据不足时如实说明，不以自检sufficient或已存在一些业务信息为由强行确定路径、风险或义务。
关键前提不足以支持本次个案判断时，risk_level为insufficient_evidence、legal_path留空，正文仍可给有依据的条件分支。范围外缺口不阻塞已经确定的限定结论。不得用medium/high/low代替未知，也不得增加材料未提供的人数、持续时间或身份。
结论、摘要、理由、清单和建议在明确关联的条件下保持一致，不必逐句重复条件；不能用末段的待确认事项挽救前面的无条件结论。区分处理依据、程序机制和具体义务，各自结合适用条件及例外。参考标准与政策解释可支持说明或建议，不独立创设法定义务。
claims关联关键法律判断所在的完整段落，不要求逐句拆分；尽量保留正文原表述以便定位。supporting_chunk_ids只复制实际取得且can_cite_clause=true的原文标识，按实际支持关系选择，不为分散来源更换引用。正文实际使用的解释、标准等资料登记reference_chunk_ids；没有使用的资料不登记。编号、来源信息及展示位置由程序处理，不自行编排引用编号或HTML标记。
decision_summary为单段纯文本，直接回答当前目标，保留决定性限制，不引入正文与材料没有的新事实。建议、待确认事实和已确认法律问题分别表达，不为填满栏目生成整改事项。仅依据当前中国数据合规语料，问题超出范围或缺少适用法源时说明限制，不补造其他法域规则。
"""
    system += "review_date是审查时点，current_date是运行日期。法律依据缺口写legal_basis_gaps，不让申请人确认法条内容或规则适用关系。\n"
    system += "完整业务待确认清单写questions，每个问题及回答类型只填一次；issues通过question_indexes关联，只由questions生成事实确认任务。\n"
    system += "output_schema=" + json.dumps(output_model.model_json_schema(), ensure_ascii=False)
    return [ChatMessage(role="system", content=system),
            ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False))]


def build_review_result_with_deepseek(
    *,
    review_result_id: str,
    review_case_id: str,
    trace_id: str,
    facts: ReviewFacts,
    self_check: EvidenceSelfCheck,
    evidence_hits: list[RetrievalHit],
    chunks_by_id: dict[str, Chunk] | None = None,
    question: str | None = None,
    material_text: str | None = None,
    retrieval_queries: list[RetrievalQuery] | None = None,
    second_retrieval: dict[str, object] | None = None,
    source_evidence_packets: list[SourceEvidencePacket] | None = None,
    client: OpenAICompatibleClient | None = None,
    max_retries: int | None = None,
) -> ReviewResult:
    """Build a governed ReviewResult using DeepSeek for result content."""

    if chunks_by_id is None:
        chunks_by_id = {}
    if client is None:
        client = OpenAICompatibleClient(require_llm_config())

    node_mode = None
    output_model = LLMReviewResultDraft

    node = StructuredLLMNode(
        node_name="result_generation",
        output_model=output_model,
        client=client,
        max_retries=max_retries,
        trace_id=trace_id,
        structured_output_mode=node_mode,
    )
    try:

        def validate_draft_grounding(draft_to_validate):
            validate_legal_basis_gaps(draft_to_validate)
            if draft_to_validate.risk_level == "insufficient_evidence" and draft_to_validate.legal_path is not None:
                raise ValueError("证据不足时不能确定法律路径")
            validated_claims = validate_grounded_claims(
                draft_to_validate.claims,
                evidence_hits,
            )
            report_evidence(evidence_hits, claims=validated_claims,
                            reference_chunk_ids=draft_to_validate.reference_chunk_ids,
                            additional_chunk_ids=[cid for issue in getattr(draft_to_validate, "issues", [])
                                                  for cid in issue.supporting_chunk_ids])
            complete_review = draft_to_validate.conclusion

            supported_summary_text = "\n".join([
                complete_review,
                material_text or "",
                json.dumps(facts.model_dump(), ensure_ascii=False),
                *[f"{hit.title}\n{hit.text}" for hit in evidence_hits],
            ])
            decision_summary = validate_decision_summary(
                draft_to_validate.decision_summary,
                supported_text=supported_summary_text,
            )
            return draft_to_validate.model_copy(
                update={
                    "claims": validated_claims,
                    "decision_summary": decision_summary,
                }
            )

        draft = node.run(
            build_result_generation_messages(
                facts=facts,
                self_check=self_check,
                evidence_hits=evidence_hits,
                question=question,
                material_text=material_text,
                retrieval_queries=retrieval_queries,
                second_retrieval=second_retrieval,
                source_evidence_packets=source_evidence_packets,
            ),
            post_validate=validate_draft_grounding,
            post_validation_reason="claim_grounding_validation_failed",
            post_validation_hint=(
                "请确保每个 claim 至少引用一个允许且可作为法条依据的 supporting_chunk_id。"
            ),
        )
        selected = report_evidence(evidence_hits, claims=draft.claims,
                                   reference_chunk_ids=draft.reference_chunk_ids,
                                   additional_chunk_ids=[cid for issue in getattr(draft, "issues", [])
                                                         for cid in issue.supporting_chunk_ids])
        citation_groups, _ = group_citations(selected, facts, chunks_by_id)
        citation_ref_by_chunk_id = {c.chunk_id: c.citation_ref for g in citation_groups for c in g.citations}
        decision_summary = draft.decision_summary.strip()
        conclusion = draft.conclusion
        claims = validate_grounded_claims(draft.claims, evidence_hits)
        claims = attach_citation_refs(claims, citation_groups)
        conclusion = inject_citation_markers(conclusion, claims, citation_ref_by_chunk_id)
        trigger_reasons = draft.trigger_reasons
        risk_level = draft.risk_level
        missing_information = draft.missing_information
        missing_answer_types = draft.missing_answer_types
        recommended_actions = draft.recommended_actions
        risk_boundaries = draft.risk_boundaries
    except ValueError as exc:
        raise ReviewWorkflowFailed(
            failed_node="result_generation",
            reason="claim_grounding_validation_failed",
            message=str(exc),
            attempts=1,
            trace_id=trace_id,
        ) from exc

    # The structured draft owns the disclaimer in its risk-boundary/report
    # section. Do not append a second fixed sentence to every conclusion.
    conclusion = conclusion.rstrip()

    all_citations: list[Citation] = []
    for group in citation_groups:
        all_citations.extend(group.citations)

    return ReviewResult(
        review_result_id=review_result_id,
        review_case_id=review_case_id,
        trace_id=trace_id,
        risk_level=risk_level,
        legal_path=draft.legal_path,
        decision_summary=decision_summary,
        conclusion=conclusion,
        review_facts=facts,
        trigger_reasons=trigger_reasons,
        missing_information=missing_information,
        legal_basis_gaps=draft.legal_basis_gaps,
        missing_answer_types=missing_answer_types,
        recommended_actions=recommended_actions,
        risk_boundaries=risk_boundaries,
        claims=claims,
        citations=all_citations,
        applicable_evidence=citation_groups,
    )
