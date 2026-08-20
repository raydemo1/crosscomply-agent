"""LLM-driven structured review result builder.

Generates governed ``ReviewResult`` via DeepSeek with Pydantic strict
validation. Includes schema definitions, prompt builders, evidence
grounding validation, revision logic, and citation injection.  Risk
level, conclusion, claims, trigger reasons, recommended actions, and
risk boundaries are all produced by the LLM under schema constraints —
there is no rule-based fallback path.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import Field, model_validator

from law_agent.config import require_llm_config
from law_agent.data.schemas import Chunk, StrictModel
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.review.citations import group_citations
from law_agent.review.llm import ReviewWorkflowFailed, StructuredLLMNode
from law_agent.review.schemas import (
    Citation,
    CitationGroup,
    ClaimReplacement,
    EvidenceDossier,
    EvidenceSelfCheck,
    GroundedClaim,
    IssuePlan,
    RetrievalHit,
    RetrievalQuery,
    ReviewFacts,
    ReviewResult,
    ReviewResultPatch,
    RevisionAction,
    RiskLevel,
    SourceEvidencePacket,
)


class LLMReviewResultDraft(StrictModel):
    """Required-field schema for LLM structured review generation (plain path)."""

    risk_level: RiskLevel
    decision_summary: str = Field(min_length=40, max_length=240)
    conclusion: str
    claims: list[GroundedClaim] = Field(default_factory=list)
    trigger_reasons: list[str]
    missing_information: list[str]
    recommended_actions: list[str]
    risk_boundaries: list[str]

    @model_validator(mode="after")
    def claims_required_unless_abstaining(self) -> LLMReviewResultDraft:
        if self.risk_level != "insufficient_evidence" and not self.claims:
            raise ValueError("non-abstention result requires grounded claims")
        return self


class MarkdownReviewDraft(StrictModel):
    """Simplified schema for markdown (frontend) path.

    Unlike ``LLMReviewResultDraft`` which splits the report into
    conclusion/recommended_actions/risk_boundaries/missing_information,
    this schema collapses everything into a single ``report`` field.
    The LLM writes one coherent markdown report with scene-adaptive
    sections, instead of filling separate fields that the backend then
    stitches back together. ``claims`` stays separate so evidence
    grounding is preserved for the right-panel citation cards.
    """

    risk_level: RiskLevel
    decision_summary: str = Field(min_length=40, max_length=240)
    report: str
    claims: list[GroundedClaim] = Field(default_factory=list)
    trigger_reasons: list[str]

    @model_validator(mode="after")
    def claims_required_unless_abstaining(self) -> MarkdownReviewDraft:
        if self.risk_level != "insufficient_evidence" and not self.claims:
            raise ValueError("non-abstention report requires grounded claims")
        return self


def validate_grounded_claims(
    claims: list[GroundedClaim],
    evidence_hits: list[RetrievalHit],
) -> list[GroundedClaim]:
    """Ensure every claim support id points at a citable evidence chunk.

    Two-level validation:

    1. The chunk_id must exist in the current evidence set (anti-hallucination).
    2. The referenced chunk must be ``can_cite_clause=True`` — i.e. a
       concrete legal article from a primary source. Guide/template/Q&A
       and other non-citable chunks are silently dropped from claim
       references; they remain in the evidence panel as auxiliary evidence
       but cannot be inlined as clause citations in the conclusion.

    Claims that only restate material facts may legitimately have no legal
    chunk support, so they are omitted from the grounded-claim rail. When
    every emitted claim loses support the result degrades to an empty
    claim list (the workflow still produces a conclusion with risk level
    and citations) rather than crashing the entire review.
    """

    allowed_ids = {hit.chunk_id for hit in evidence_hits}
    citable_ids = {hit.chunk_id for hit in evidence_hits if hit.can_cite_clause}
    if not citable_ids:
        return []
    cleaned: list[GroundedClaim] = []
    for claim in claims:
        valid_ids = [
            cid for cid in claim.supporting_chunk_ids if cid in allowed_ids and cid in citable_ids
        ]
        if not valid_ids:
            continue
        cleaned.append(claim.model_copy(update={"supporting_chunk_ids": valid_ids}))
    return cleaned


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


def _sanitize_markdown_text(text: str) -> str:
    """Gently clean LLM-generated markdown so the frontend renders safely.

    - Downgrade ``#`` / ``##`` headings to ``###`` (page owns h1/h2).
    - Strip fenced code block fences.
    - Repair unpaired ``**`` by dropping the last occurrence.
    - Un-bold spans longer than 50 chars (LLM sometimes bolds a whole
      sentence against the prompt; the frontend bold style is intended
      for short legal-term emphasis only).

    Plain text (rule builder output) passes through unchanged because it
    contains no markdown markers.
    """
    if not text:
        return text

    text = _CODE_FENCE_RE.sub("", text)
    text = _HEADING_DOWNGRADE_RE.sub(r"### ", text)

    # Repair unpaired ** : if count is odd, remove the last **.
    bold_count = text.count("**")
    if bold_count % 2 == 1:
        idx = text.rfind("**")
        text = text[:idx] + text[idx + 2 :]

    # Un-bold overly long spans (whole-sentence bolding).
    def _unbold_long(match: re.Match[str]) -> str:
        inner = match.group(1)
        if len(inner) > 50:
            return inner
        return match.group(0)

    text = _BOLD_SPAN_RE.sub(_unbold_long, text)
    return text


def _validate_decision_summary(summary: str, *, supported_text: str) -> str:
    """Keep the approval summary plain and bounded by reviewed material/evidence."""

    value = summary.strip()
    if re.search(r"[\n\r#*_`]", value):
        raise ValueError("decision_summary must be one plain-text paragraph")
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

# Match legal article references like "《个人信息保护法》第三十九条" or
# standalone "第三十九条" / "第七条" in the report text.
_ARTICLE_REF_RE = re.compile(r"(?:《[^》]+》)?\s*第[一二三四五六七八九十百零〇\d]+条")


def _extract_cite_phrase(claim_text: str) -> str | None:
    """Extract the most specific legal-article phrase from a claim.

    Preference order:
    1. Full ``《法律名》第X条`` form (most precise).
    2. Bare ``第X条`` form.
    3. ``None`` if no article reference found (caller will fall back to
       appending the marker at the end of the nearest sentence).
    """

    if not claim_text:
        return None
    full_match = re.search(r"《[^》]+》\s*第[一二三四五六七八九十百零〇\d]+条", claim_text)
    if full_match:
        return full_match.group(0)
    bare_match = re.search(r"第[一二三四五六七八九十百零〇\d]+条", claim_text)
    if bare_match:
        return bare_match.group(0)
    return None


def _compact_markdown_text(value: str) -> str:
    """Normalize inline markdown so a claim can be located in its report."""

    without_tags = re.sub(r"<[^>]+>", "", value or "")
    without_formatting = re.sub(r"[*_`~]", "", without_tags)
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", without_formatting, flags=re.UNICODE)


def _claim_terms(value: str) -> set[str]:
    """Extract stable two-character terms for approximate paragraph matching."""

    without_law_ref = re.sub(r"《[^》]+》", "", value or "")
    without_law_ref = re.sub(r"第[一二三四五六七八九十百零〇\d]+条", "", without_law_ref)
    without_formatting = re.sub(r"[*_`~]", "", without_law_ref)
    terms: set[str] = set()
    for run in re.findall(r"[\u4e00-\u9fff]{2,}", without_formatting):
        terms.update(run[index : index + 2] for index in range(len(run) - 1))
    terms.update(token.casefold() for token in re.findall(r"[A-Za-z0-9]{2,}", without_formatting))
    return terms


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


def _find_claim_paragraph_end(report: str, claim_text: str) -> int | None:
    """Find the end of the paragraph containing a grounded claim.

    Claim text often differs from the report only by markdown emphasis or
    punctuation. Compact matching handles those presentation differences but
    still requires the claim's substantive text to be present before attaching
    a marker to the paragraph.
    """

    compact_claim = _compact_markdown_text(claim_text)
    if len(compact_claim) < 8:
        return None
    for start, end in _report_paragraph_ranges(report):
        if compact_claim in _compact_markdown_text(report[start:end]):
            return end

    claim_terms = _claim_terms(claim_text)
    if len(claim_terms) < 4:
        return None
    ranges = _report_paragraph_ranges(report)
    if _extract_cite_phrase(claim_text):
        prose_ranges = [
            (start, end)
            for start, end in ranges
            if not re.match(r"(?:[-*]\s+|\d+[.、)]\s*)", report[start:end].lstrip())
        ]
        if prose_ranges:
            ranges = prose_ranges
    best_overlap = 0
    best_end: int | None = None
    for start, end in ranges:
        overlap = len(claim_terms & _claim_terms(report[start:end]))
        if overlap > best_overlap:
            best_overlap = overlap
            best_end = end
    if best_overlap >= 4:
        return best_end
    return None


def inject_citation_markers(
    report: str,
    claims: list[GroundedClaim],
    evidence_hits: list[RetrievalHit] | None = None,
    citation_ref_by_chunk_id: dict[str, str] | None = None,
) -> str:
    """Inject ①②③ markers into the report text for each claim.

    For each claim (in order), find the first occurrence of its cite phrase
    in the report and insert a ``<sup>`` marker right after it.

    The cite phrase is resolved with this priority:
    1. The ``citation_label`` of the claim's first supporting chunk (most
       precise — e.g. "《个人信息保护法》第三十九条").
    2. The ``article_no`` of the supporting chunk (e.g. "第三十九条").
    3. A ``《法律名》第X条`` phrase extracted from the claim text itself.

    If the exact legal phrase cannot be matched, the marker is placed at the
    end of the paragraph that contains the grounded claim. It is never emitted
    as a separate explanation block.
    """

    if not report or not claims:
        return report

    # Build chunk_id -> RetrievalHit lookup for citation_label/article_no.
    chunks_by_id: dict[str, RetrievalHit] = {}
    if evidence_hits:
        for hit in evidence_hits:
            chunks_by_id[hit.chunk_id] = hit

    # Track which report positions have already been marked to avoid
    # stacking multiple markers on the same phrase.
    marked_positions: set[int] = set()
    text = report
    for index, claim in enumerate(claims[: len(_CIRCLED_NUMBERS)]):
        marker = _CIRCLED_NUMBERS[index]
        citation_ref = next(
            (
                citation_ref_by_chunk_id.get(chunk_id)
                for chunk_id in claim.supporting_chunk_ids
                if citation_ref_by_chunk_id and citation_ref_by_chunk_id.get(chunk_id)
            ),
            None,
        )
        # Resolve the cite phrase: prefer chunk citation_label, then
        # article_no, then fall back to extracting from claim text.
        phrase: str | None = None
        if claim.supporting_chunk_ids and chunks_by_id:
            primary_chunk = chunks_by_id.get(claim.supporting_chunk_ids[0])
            if primary_chunk:
                if primary_chunk.citation_label:
                    phrase = primary_chunk.citation_label
                elif primary_chunk.article_no:
                    phrase = primary_chunk.article_no
        if not phrase:
            phrase = _extract_cite_phrase(claim.text)

        inserted = False
        claim_paragraph_end = _find_claim_paragraph_end(text, claim.text)
        # Claims without an explicit article reference are narrative claims;
        # prefer their own paragraph over a matching article number elsewhere.
        if not _extract_cite_phrase(claim.text) and claim_paragraph_end is not None:
            ref_attr = f' data-citation-ref="{citation_ref}"' if citation_ref else ""
            sup = (
                f'<sup class="cite-marker" id="cite-marker-{index}"'
                f' data-claim-index="{index}"{ref_attr}>{marker}</sup>'
            )
            text = text[:claim_paragraph_end] + sup + text[claim_paragraph_end:]
            inserted = True
        if phrase:
            # citation_label is stored as "法律名 第X条" (no 《》, space
            # separated), but the report typically writes "《法律名》**第X条**"
            # with book-title marks and bold. Build a tolerant regex:
            #   - law name may be wrapped in 《》
            #   - ** may appear anywhere around the article number
            #   - whitespace allowed between name and number
            # Split the phrase into (law_name, article_no) if possible.
            article_match = re.search(r"第[一二三四五六七八九十百零〇\d]+条", phrase)
            if article_match:
                article_part = article_match.group(0)
                law_name = phrase[: article_match.start()].strip().rstrip("》").lstrip("《").strip()
                if law_name:
                    # Match optional 《》, law name, optional 》, optional **,
                    # then article number, then optional **.
                    pattern = re.compile(re.escape(article_part))
                    # First try the full precise form with law name.
                    full_pattern = re.compile(
                        r"《?"
                        + re.escape(law_name)
                        + r"》?\s*\**\s*"
                        + re.escape(article_part)
                        + r"\**"
                    )
                    patterns_to_try = [full_pattern, pattern]
                else:
                    patterns_to_try = [re.compile(re.escape(article_part) + r"\**")]
            else:
                patterns_to_try = [re.compile(re.escape(phrase))]

            search_start = 0
            for pat in patterns_to_try:
                if inserted:
                    break
                while True:
                    match = pat.search(text, search_start)
                    if match is None:
                        break
                    insert_at = match.end()
                    # Skip any trailing ** so the marker lands after the bold close.
                    while insert_at < len(text) and text[insert_at] == "*":
                        insert_at += 1
                    if insert_at not in marked_positions:
                        ref_attr = f' data-citation-ref="{citation_ref}"' if citation_ref else ""
                        sup = (
                            f'<sup class="cite-marker" id="cite-marker-{index}"'
                            f' data-claim-index="{index}"{ref_attr}>{marker}</sup>'
                        )
                        text = text[:insert_at] + sup + text[insert_at:]
                        marked_positions.add(insert_at)
                        inserted = True
                        break
                    search_start = match.end() + 1
        if not inserted:
            paragraph_end = _find_claim_paragraph_end(text, claim.text)
            if paragraph_end is not None:
                ref_attr = f' data-citation-ref="{citation_ref}"' if citation_ref else ""
                sup = (
                    f'<sup class="cite-marker" id="cite-marker-{index}"'
                    f' data-claim-index="{index}"{ref_attr}>{marker}</sup>'
                )
                text = text[:paragraph_end] + sup + text[paragraph_end:]
                inserted = True
    return text


def _remove_citation_markers(report: str) -> str:
    """Remove generated inline markers before rebuilding a revised report."""

    return re.sub(
        r'<sup\b[^>]*class=["\']cite-marker["\'][^>]*>.*?</sup>',
        "",
        report,
        flags=re.DOTALL,
    )


def _sanitize_draft_markdown(draft: LLMReviewResultDraft) -> LLMReviewResultDraft:
    """Apply markdown sanitization to all text fields of an LLM result draft.

    Kept for potential future use on the plain path; the markdown path now
    uses ``MarkdownReviewDraft`` and sanitizes only the ``report`` field
    inline (see ``build_review_result_with_deepseek``).
    """

    return draft.model_copy(
        update={
            "conclusion": _sanitize_markdown_text(draft.conclusion),
            "claims": [
                claim.model_copy(update={"text": _sanitize_markdown_text(claim.text)})
                for claim in draft.claims
            ],
            "recommended_actions": [_sanitize_markdown_text(a) for a in draft.recommended_actions],
            "risk_boundaries": [_sanitize_markdown_text(b) for b in draft.risk_boundaries],
            "missing_information": [_sanitize_markdown_text(m) for m in draft.missing_information],
        }
    )


def _extract_markdown_section_items(report: str, section_titles: set[str]) -> list[str]:
    """Extract ordered or bulleted items from a governed report section.

    The frontend-facing markdown path intentionally keeps the report as one
    coherent document.  The workbench still needs durable action records, so
    this small parser promotes the explicitly labelled action section into
    ``ReviewResult.recommended_actions`` without trying to infer actions from
    arbitrary prose.
    """

    headings = list(re.finditer(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*$", report))
    target_start: int | None = None
    target_end = len(report)
    for index, match in enumerate(headings):
        title = re.sub(r"[：:]\s*$", "", match.group(1).strip())
        if title in section_titles:
            target_start = match.end()
            if index + 1 < len(headings):
                target_end = headings[index + 1].start()
            break
    if target_start is None:
        return []

    actions: list[str] = []
    for line in report[target_start:target_end].splitlines():
        item = re.match(r"^\s*(?:[-*•]|\d+[.)、．])\s+(.+?)\s*$", line)
        if not item:
            continue
        value = re.sub(r"(?:\*\*|__|`)", "", item.group(1)).strip()
        value = re.sub(r"\s+", " ", value)
        if value and value not in actions:
            actions.append(value)
    return actions


# ---------------------------------------------------------------------------
# Full result builder
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# DeepSeek structured result generation
# ---------------------------------------------------------------------------


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
    issue_plan: IssuePlan | None = None,
    evidence_dossiers: list[EvidenceDossier] | None = None,
    output_format: Literal["plain", "markdown"] = "plain",
    critique_instructions: list[str] | None = None,
) -> list[ChatMessage]:
    """Build a DeepSeek JSON prompt for structured review result generation.

    ``output_format="plain"`` (default, used by eval) emits plain-text
    conclusion/claims — keeps eval metrics stable and lets strict_tool mode
    validate the schema cleanly. ``output_format="markdown"`` (used by the
    frontend API) emits markdown-formatted conclusion (### headings, **bold**
    legal terms, lists) and inline-bold claims for richer display. Both
    formats share the same evidence/facts/corpus_scope payload and the same
    risk_level / abstention logic, so retrieval and risk judgements stay
    consistent across the two paths.
    """

    if output_format == "markdown":
        # markdown path uses MarkdownReviewDraft schema: a single `report`
        # field carries the whole review as natural-language markdown,
        # instead of splitting into conclusion/actions/boundaries/missing.
        # The LLM writes one coherent report whose sections adapt to the
        # scene (cross-border / sensitive info / insufficient evidence…).
        json_example = {
            "risk_level": "medium",
            "decision_summary": (
                "当前材料显示该境外 CRM 场景属于个人信息出境，但企业并非关键信息基础设施运营者、"
                "未涉及重要数据，现有数量未达到安全评估门槛，可优先采用标准合同或认证路径。"
                "上线前仍须核实敏感信息范围，完成个人信息保护影响评估、标准合同签署及备案。"
            ),
            "report": (
                "### 风险定性\n"
                "该场景涉及**个人信息跨境提供**，存在**中等合规风险**。\n\n"
                "### 关键法律依据\n"
                "依据《个人信息保护法》**第三十八条**，个人信息出境需通过安全评估、"
                "标准合同或认证等法定路径；《数据出境安全评估办法》规定，处理100万人"
                "以上个人信息的数据处理者应当申报安全评估。材料显示拟向境外接收方传输"
                "员工个人信息，但未说明已采取的出境路径。\n\n"
                "### 合规义务与缺口\n"
                "- 需确认是否取得用户**单独同意**；\n"
                "- 需确认出境数据规模是否触发**数据出境安全评估**申报门槛；\n"
                "- 材料未提供境外接收方信息，影响风险完整性判断。\n\n"
                "### 建议措施\n"
                "1. 取得用户**单独同意**并留存告知记录；\n"
                "2. 与境外接收方签订**数据处理协议**；\n"
                "3. 评估是否需要申报**数据出境安全评估**。\n\n"
                "### 风险边界\n"
                "本结论基于当前材料和已召回证据，**不构成正式法律意见**；"
                "如出境方式或接收方变更需重新评估。"
            ),
            "claims": [
                {
                    "text": "该场景涉及**个人信息跨境提供**，存在中等合规风险。",
                    "supporting_chunk_ids": ["chunk_id_from_evidence_packets"],
                },
                {
                    "text": "依据《个人信息保护法》**第三十八条**，出境需通过法定路径。",
                    "supporting_chunk_ids": ["another_chunk_id_from_evidence_packets"],
                },
                {
                    "text": "需确认是否取得**单独同意**及数据出境规模。",
                    "supporting_chunk_ids": ["chunk_id_from_evidence_packets"],
                },
            ],
            "trigger_reasons": ["cross_border_transfer", "missing_information"],
        }
        # markdown path: 3 replaced instructions adapt the shared list to
        # the report-based schema. Extra instructions teach report format
        # and chunk diversification. Plain path stays byte-identical to HEAD.
        format_instruction_replacements = [
            "必须输出合法 json object，字段必须与 json_example 完全一致；decision_summary 使用纯文本，report 字段内使用 markdown 符号（**、###、-、数字列表）。",
            "claims 必须逐句覆盖 report 中的关键判断；每个 claim.text 是一个可单独展示的结论句，关键法律术语可用 **加粗**（只加粗短语，不加粗整句）。",
            "只输出 json object，不要输出 json 以外的任何解释文字；report 内可自由使用 markdown 让内容更易读。",
        ]
        extra_markdown_instructions = [
            "decision_summary 是供飞书审批和 PDF 首页共用的审批摘要，用 120-200 个中文字符直接回答问题，并概括风险等级、建议决定、核心依据和上线前 2-4 项关键条件；使用单段纯文本，不要使用 markdown、引用标记或免责声明，不得引入 report 和 evidence_packets 中没有的新事实、法条或数字。",
            "report 用 markdown 输出完整审查报告，根据场景自适应选择小节，通常包含「风险定性」「关键法律依据」「合规义务与缺口」「建议措施」「风险边界」等 ### 小节；段落间空行分隔，关键法律依据短语用 **加粗**，合规义务用 - 列表，建议措施用数字列表；不要用 # 或 ## 标题，不要用 ``` 代码块，长度建议 250-500 字。",
            "语言表达要自然清晰，让业务人员能快速看懂合规风险、义务缺口和下一步动作；不要堆砌法条，要把规则落到当前材料的实际场景上。",
            "claims 的 supporting_chunk_ids 只能从 payload.citable_chunk_ids 中选取（这些是 can_cite_clause=true 的法条 chunk）；不要使用 citable_chunk_ids 以外的任何 chunk_id，evidence_packets 中 can_cite_clause=false 的指南/范本/Q&A/地方清单只能作为背景理解，不能出现在 supporting_chunk_ids 里。",
            "claims 优先从 evidence_packets[].supporting_chunks 中选取条款最精确的 chunk，不要反复引用同一个 representative_chunk；不同 claim 尽量引用不同 chunk 以分散证据来源。",
            "每个 claim.text 应明确包含所依据的法律条款编号（如「《个人信息保护法》第三十九条」「数据出境安全评估办法 第七条」），便于生成内联引用标记。",
        ]
        system_content = (
            "你是企业数据合规审查结果生成助手。"
            "只输出一个合法 json object，不要输出 json 以外的任何解释文字；"
            "decision_summary 输出审批人可直接阅读的纯文本摘要，"
            "report 字段用 markdown 输出清晰易懂的完整审查报告。"
        )
        # markdown path has no missing_information/actions/boundaries fields
        # (they live inside report), so instruction 5 must be adapted.
        missing_facts_instruction = (
            "缺失的事实和合规缺口直接写进 report 的「合规义务与缺口」「建议措施」"
            "等小节；不要因为存在信息缺口就输出 insufficient_evidence。"
        )
    else:
        json_example = {
            "risk_level": "medium",
            "decision_summary": (
                "当前材料显示该场景可能涉及个人信息跨境提供，但数据规模、敏感信息范围和同意情况"
                "尚未完整确认。现阶段可形成中风险的有边界判断，需先补齐关键事实并核对是否触发"
                "安全评估门槛，再确定标准合同、认证或安全评估路径。"
            ),
            "conclusion": "该场景可能涉及个人信息跨境提供，但仍需补充数据规模和同意情况。",
            "claims": [
                {
                    "text": "该场景可能涉及个人信息跨境提供。",
                    "supporting_chunk_ids": ["chunk_id_from_evidence_packets"],
                },
                {
                    "text": "仍需补充数据规模和同意情况。",
                    "supporting_chunk_ids": ["another_chunk_id_from_evidence_packets"],
                },
            ],
            "trigger_reasons": ["cross_border_transfer", "missing_information"],
            "missing_information": ["legal_basis_or_consent", "data_volume_threshold"],
            "recommended_actions": ["确认是否取得单独同意", "确认出境数据规模"],
            "risk_boundaries": ["本结论基于当前材料和已召回证据，不构成正式法律意见"],
        }
        # plain path: keep HEAD instructions exactly as-is (eval stability).
        format_instruction_replacements = [
            "必须输出合法 json object，字段必须与 json_example 完全一致；decision_summary 使用纯文本。",
            "claims 必须逐句覆盖 conclusion 中的关键判断；每个 claim.text 是一个可单独展示的结论句。",
            "不要输出解释、markdown 或自然语言。",
        ]
        extra_markdown_instructions = [
            "decision_summary 是供审批使用的摘要，用 120-200 个中文字符直接回答问题，并概括风险等级、建议决定、核心依据和 2-4 项关键条件；不要使用 markdown，不得引入 conclusion 和 evidence_packets 中没有的新事实、法条或数字。",
        ]
        system_content = (
            "你是企业数据合规审查结果生成助手。只输出 json，不输出解释、markdown 或自然语言。"
        )
        # plain path keeps the HEAD instruction 5 verbatim.
        missing_facts_instruction = (
            "缺失事实优先写入 missing_information、recommended_actions 和 risk_boundaries；"
            "不要仅因存在 missing_information 就输出 insufficient_evidence。"
        )
    evidence_packets = [
        {
            "source_id": packet.source_id,
            "title": packet.title,
            "representative_chunk": _llm_evidence_hit(packet.representative_chunk),
            "supporting_chunks": [_llm_evidence_hit(hit) for hit in packet.supporting_chunks[:2]],
            "neighbor_chunks": [_llm_evidence_hit(hit) for hit in packet.neighbor_chunks[:2]],
        }
        for packet in (source_evidence_packets or [])
    ]
    # Flat whitelist of every chunk_id the LLM is allowed to cite. LLM
    # occasionally hallucinates ids that follow the source_id:N pattern
    # (e.g. ``:0004`` when only 0000/0003/0005 exist) by pattern-completion
    # on the nested evidence_packets structure. Surfacing the full list at
    # the top of the payload gives the model a single source of truth to
    # copy from. This field is purely additive context — no instruction
    # text is changed, so eval metrics stay stable.
    allowed_chunk_ids = sorted({hit.chunk_id for hit in evidence_hits})
    # Citable-only whitelist: claims[].supporting_chunk_ids must come from
    # this list. Pre-filtering here means the LLM does not have to inspect
    # each chunk's can_cite_clause flag — it just copies ids from this list.
    # Non-citable chunks (guides/templates/Q&A/local lists) remain in
    # evidence_packets as background context but cannot be cited.
    citable_chunk_ids = sorted({hit.chunk_id for hit in evidence_hits if hit.can_cite_clause})
    payload = {
        "question": question,
        "material_excerpt": (material_text or "")[:3000],
        "allowed_chunk_ids": allowed_chunk_ids,
        "citable_chunk_ids": citable_chunk_ids,
        "review_facts": facts.model_dump(),
        "evidence_self_check": self_check.model_dump(),
        "retrieval_queries": [query.model_dump() for query in (retrieval_queries or [])],
        "second_retrieval": second_retrieval or {},
        "evidence_packets": evidence_packets,
        "corpus_scope": {
            "jurisdiction": "中国大陆数据合规和个人信息保护语料",
            "includes": [
                "个人信息保护法、数据安全法、网络安全法、网络数据安全管理条例",
                "数据出境安全评估、个人信息出境标准合同、个人信息保护认证",
                "国家网信部门政策问答、TC260 标准、自贸区地方数据出境清单、汽车/金融等行业材料",
            ],
            "excludes": [
                "EU AI Act",
                "CCPA/CPRA",
                "其他外国法或非数据合规领域问题",
            ],
        },
        "json_example": json_example,
        "critique_instructions": critique_instructions or [],
        "instructions": [
            "基于审查事实和 evidence_packets 生成结构化审查结果。",
            "必须结合 question、material_excerpt、retrieval_queries 和 evidence_self_check 判断结论边界。",
            "只能基于 corpus_scope 内的中国数据合规语料和 evidence_packets 作答；如果 question 明确询问 corpus_scope.excludes 中的法域或制度，risk_level 必须为 insufficient_evidence。",
            "不要过度谨慎：如果材料已有可审查事实且 evidence 支持相关规则，即使缺少数据规模、同意状态、备案细节等信息，也要给出 high、medium 或 low 的有边界判断。",
            missing_facts_instruction,
            "如果材料没有说明关键事实（数据类型、处理目的、是否出境、接收方、地区/行业等），不要把 question 中的假设当作事实；只有在无法形成任何有用边界判断时才输出 insufficient_evidence。",
            "当 evidence_self_check.status 为 sufficient，且材料至少包含一个实质法律维度（如数据类型、处理目的、跨境安排、地区、行业、个人信息/敏感信息），通常不应输出 insufficient_evidence。",
            format_instruction_replacements[0],
            "risk_level 只能是 high、medium、low、insufficient_evidence。",
            format_instruction_replacements[1],
            *extra_markdown_instructions,
            "每个 claims[].supporting_chunk_ids 必须只使用 evidence_packets 中真实存在的 chunk_id，且不能为空。",
            "evidence_self_check.status=sufficient 表示证据可用于当前语料范围内的判断；若事实有缺口但仍可形成边界判断，不要拒答。",
            "insufficient_evidence 只适用于：证据自检 insufficient、材料几乎没有可审查事实、问题超出 corpus_scope、或证据与问题/材料明显不匹配。",
            "不得编造未出现在证据中的法律来源。",
            "优先依据 representative_chunk；supporting_chunks 用于补充同一来源内更精确的条款；neighbor_chunks 只用于理解上下文。",
            "引用分组由程序处理，本节点不要输出 citations。",
            "如果 critique_instructions 非空，必须逐条修正，但不得引入 evidence_packets 之外的新依据。",
            format_instruction_replacements[2],
        ],
    }
    if issue_plan is not None:
        system_content += (
            "当前处于 multi-agent 工作流：你是最终的 Compliance Reviewer，"
            "应根据已交接的议题计划和证据工作包完成报告，而不是重新规划检索。"
        )
        payload["issue_plan"] = [issue.model_dump() for issue in issue_plan.issues]
        payload["evidence_dossiers"] = [
            dossier.model_dump() for dossier in (evidence_dossiers or [])
        ]
        payload["instructions"].append(
            "multi-agent 模式：你是 Compliance Reviewer。issue_plan 是必须逐项完成的审查"
            "清单，不要重新拆分或忽略其中的问题；有两个及以上独立 issue 时，应让业务读者"
            "能在报告中区分各问题的判断。evidence_dossiers 仅是 Researcher 的交接："
            "coverage_status=covered 时给出有边界的判断；partial 或 missing 时明确写出该问题"
            "尚不能确定的条件、证据缺口和下一步核验，不得把缺口推断为事实或确定结论。"
            "dossier 中的 chunk_id 不是可引用依据；claims 的 supporting_chunk_ids 仍只能引用"
            "evidence_packets 中允许的原始条文。"
        )
    return [
        ChatMessage(
            role="system",
            content=system_content,
        ),
        ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
    ]


def _llm_evidence_hit(hit: RetrievalHit) -> dict[str, object]:
    return {
        "chunk_id": hit.chunk_id,
        "source_id": hit.source_id,
        "title": hit.title,
        "text": hit.text[:1200],
        "citation_role": hit.citation_role,
        "can_cite_clause": hit.can_cite_clause,
        "article_no": hit.article_no,
        "citation_label": hit.citation_label,
        "source_url": hit.source_url,
        "score": hit.score,
        "rank": hit.rank,
        "matched_query_type": hit.matched_query_type,
    }


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
    issue_plan: IssuePlan | None = None,
    evidence_dossiers: list[EvidenceDossier] | None = None,
    client: OpenAICompatibleClient | None = None,
    max_retries: int | None = None,
    output_format: Literal["plain", "markdown"] = "plain",
    critique_instructions: list[str] | None = None,
) -> ReviewResult:
    """Build a governed ReviewResult using DeepSeek for result content.

    ``output_format="plain"`` (default, eval path) emits plain-text fields
    and runs under the client's default structured_output_mode (usually
    strict_tool) for tight schema validation. ``output_format="markdown"``
    (frontend path) emits markdown-formatted conclusion/claims and forces
    json_object mode, since strict_tool schema constraints clash with
    free-form markdown; normalization is still guaranteed by Pydantic
    strict validation + retry. Both paths share the same evidence/facts
    payload and risk logic, so retrieval and risk judgements stay
    consistent — only the textual rendering differs.
    """

    if chunks_by_id is None:
        chunks_by_id = {}
    if client is None:
        client = OpenAICompatibleClient(require_llm_config())

    citation_groups, _violations = group_citations(evidence_hits, facts, chunks_by_id)
    citation_ref_by_chunk_id = {
        citation.chunk_id: citation.citation_ref
        for group in citation_groups
        for citation in group.citations
        if citation.citation_ref
    }

    # markdown format forces json_object: strict_tool schema validation
    # rejects free-form markdown inside string fields. plain format falls
    # back to the client's configured mode (usually strict_tool) so eval
    # gets the tightest schema guarantee.
    node_mode = "json_object" if output_format == "markdown" else None
    # markdown path uses a simplified schema (MarkdownReviewDraft) with a
    # single `report` field; plain path uses the split-field schema.
    output_model = MarkdownReviewDraft if output_format == "markdown" else LLMReviewResultDraft

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
            validated_claims = validate_grounded_claims(
                draft_to_validate.claims,
                evidence_hits,
            )
            complete_review = getattr(
                draft_to_validate,
                "report",
                getattr(draft_to_validate, "conclusion", ""),
            )
            supported_summary_text = "\n".join([
                complete_review,
                material_text or "",
                json.dumps(facts.model_dump(), ensure_ascii=False),
                *[f"{hit.title}\n{hit.text}" for hit in evidence_hits],
            ])
            decision_summary = _validate_decision_summary(
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
                issue_plan=issue_plan,
                evidence_dossiers=evidence_dossiers,
                output_format=output_format,
                critique_instructions=critique_instructions,
            ),
            post_validate=validate_draft_grounding,
            post_validation_reason="claim_grounding_validation_failed",
        )
        # markdown path: sanitize the report text and use it as conclusion.
        # The explicitly labelled action section is also promoted into
        # durable case actions by the workbench API.
        # plain path: pass through unchanged so eval sees exactly what the
        # LLM emitted.
        if output_format == "markdown":
            md_draft: MarkdownReviewDraft = draft  # type: ignore[assignment]
            md_draft = md_draft.model_copy(
                update={"report": _sanitize_markdown_text(md_draft.report)}
            )
            decision_summary = md_draft.decision_summary.strip()
            claims = validate_grounded_claims(md_draft.claims, evidence_hits)
            claims = attach_citation_refs(claims, citation_groups)
            # Inject ①②③ inline citation markers into the report text so
            # the frontend can render clickable superscripts that link to
            # the citation cards below. Done after validation so markers
            # only cover citable legal articles. Uses chunk citation_label
            # for matching so the marker lands on the exact legal article
            # reference in the report text.
            conclusion = inject_citation_markers(
                md_draft.report,
                claims,
                evidence_hits,
                citation_ref_by_chunk_id,
            )
            trigger_reasons = md_draft.trigger_reasons
            risk_level = md_draft.risk_level
            missing_information: list[str] = []
            recommended_actions = _extract_markdown_section_items(
                md_draft.report,
                {"建议措施", "建议动作"},
            )
            risk_boundaries: list[str] = []
        else:
            plain_draft: LLMReviewResultDraft = draft  # type: ignore[assignment]
            decision_summary = plain_draft.decision_summary.strip()
            conclusion = plain_draft.conclusion
            claims = validate_grounded_claims(plain_draft.claims, evidence_hits)
            claims = attach_citation_refs(claims, citation_groups)
            trigger_reasons = plain_draft.trigger_reasons
            risk_level = plain_draft.risk_level
            missing_information = plain_draft.missing_information
            recommended_actions = plain_draft.recommended_actions
            risk_boundaries = plain_draft.risk_boundaries
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
        decision_summary=decision_summary,
        conclusion=conclusion,
        review_facts=facts,
        trigger_reasons=trigger_reasons,
        missing_information=missing_information,
        recommended_actions=recommended_actions,
        risk_boundaries=risk_boundaries,
        claims=claims,
        citations=all_citations,
        applicable_evidence=citation_groups,
    )


def build_revision_patch_messages(
    *,
    result: ReviewResult,
    actions: list[RevisionAction],
    evidence_hits: list[RetrievalHit],
    issue_plan: IssuePlan | None = None,
    evidence_dossiers: list[EvidenceDossier] | None = None,
) -> list[ChatMessage]:
    """Build a minimal-delta revision prompt with an explicit evidence inventory."""

    citable_hits = [hit for hit in evidence_hits if hit.can_cite_clause]
    payload = {
        "original_result": result.model_dump(exclude={"citations", "applicable_evidence"}),
        "revision_actions": [action.model_dump() for action in actions],
        "allowed_citable_evidence": [
            {
                "chunk_id": hit.chunk_id,
                "title": hit.title,
                "text": hit.text[:1000],
                "citation_role": hit.citation_role,
            }
            for hit in citable_hits
        ],
        "allowed_supporting_chunk_ids": [hit.chunk_id for hit in citable_hits],
        "instructions": [
            "只输出对 original_result 的最小 patch，不重新生成整份结果。",
            "remove_claim/narrow_claim 必须删除或收窄无依据结论。",
            "mark_evidence_gap 必须写入 missing_information 或 risk_boundaries。",
            "只有 add_supported_claim 才可新增 claim，且 chunk_id 必须来自白名单。",
            "不得引入 allowed_citable_evidence 中没有的新法规、条款或制度名称。",
            "无法完成的修订应收窄结论或披露缺口，不能猜测依据。",
            "若 risk_level 改为 insufficient_evidence，remove_claim_indexes 应覆盖全部原 claims，且不得新增 claims。",
            "修改 risk_level 或 conclusion 时必须同步输出新的 decision_summary；摘要使用 40-240 字单段纯文本，不得引入修订后结论、原事实和允许证据之外的新法条或数字。",
        ],
    }
    if issue_plan is not None:
        payload["issue_plan"] = [issue.model_dump() for issue in issue_plan.issues]
        payload["evidence_dossiers"] = [
            dossier.model_dump() for dossier in (evidence_dossiers or [])
        ]
        payload["instructions"].append(
            "multi-agent 模式：这是同一 Compliance Reviewer 的一次最小修订，不重写整份报告。"
            "修订后仍须保留对 issue_plan 的逐项覆盖；对 evidence_dossiers 为 partial 或 missing"
            "的问题，只能收窄结论或披露缺口。evidence_dossiers 说明补证后的问题级覆盖状态，"
            "但只能使用 allowed_citable_evidence 中的条文。"
        )
    example = {
        "risk_level": "medium",
        "decision_summary": (
            "补证后仍不足以支持原确定性判断，当前应按中风险有边界处理。"
            "审批前需补充直接法律依据并重新核对适用路径，不能依据原摘要直接放行。"
        ),
        "conclusion": "当前证据不足以作确定认定，需补充直接法律依据。",
        "remove_claim_indexes": [0],
        "replace_claims": [],
        "add_claims": [],
        "append_missing_information": ["缺少直接法律依据"],
        "append_recommended_actions": [],
        "append_risk_boundaries": ["不得将辅助材料作为法条依据"],
    }
    return [
        ChatMessage(
            role="system",
            content=(
                "你是企业数据合规审查的 Compliance Reviewer，正在执行一次受证据约束的局部修订；"
                "证据不足时删除、收窄或标记缺口，绝不补写语料中不存在的法规。"
            ),
        ),
        ChatMessage(
            role="user",
            content=(
                "输出严格 JSON。"
                f"\njson_example={json.dumps(example, ensure_ascii=False)}"
                f"\npayload={json.dumps(payload, ensure_ascii=False)}"
            ),
        ),
    ]


def _validate_revision_patch(
    patch: ReviewResultPatch,
    *,
    result: ReviewResult,
    actions: list[RevisionAction],
    evidence_hits: list[RetrievalHit],
) -> ReviewResultPatch:
    claim_count = len(result.claims)
    indexes = list(patch.remove_claim_indexes) + [
        replacement.claim_index for replacement in patch.replace_claims
    ]
    if any(index >= claim_count for index in indexes):
        raise ValueError("revision patch references an unknown claim index")
    if len(indexes) != len(set(indexes)):
        raise ValueError("revision patch modifies the same claim more than once")

    operations = {action.operation for action in actions}
    if patch.add_claims and "add_supported_claim" not in operations:
        raise ValueError("revision patch cannot add claims without an add action")
    if patch.replace_claims and not operations.intersection(
        {"narrow_claim", "add_supported_claim"}
    ):
        raise ValueError("revision patch cannot replace claims for these actions")
    if patch.remove_claim_indexes and not operations.intersection(
        {"remove_claim", "narrow_claim", "mark_evidence_gap", "abstain"}
    ):
        raise ValueError("revision patch cannot remove claims for these actions")
    if (
        patch.risk_level is not None
        and patch.risk_level != result.risk_level
        and not operations.intersection(
            {"narrow_claim", "mark_evidence_gap", "change_risk_boundary", "abstain"}
        )
    ):
        raise ValueError("revision patch cannot change risk for these actions")
    if (
        patch.risk_level == "insufficient_evidence"
        and result.risk_level != "insufficient_evidence"
        and "abstain" not in operations
    ):
        raise ValueError("only an explicit abstain action may transition to insufficient_evidence")

    allowed_ids = {hit.chunk_id for hit in evidence_hits if hit.can_cite_clause}
    proposed_claims = [replacement.claim for replacement in patch.replace_claims] + list(
        patch.add_claims
    )
    for claim in proposed_claims:
        if not claim.supporting_chunk_ids or not set(claim.supporting_chunk_ids).issubset(
            allowed_ids
        ):
            raise ValueError("revision patch claim uses unavailable legal evidence")

    requires_conclusion_change = any(
        action.operation in {"remove_claim", "narrow_claim", "mark_evidence_gap", "abstain"}
        for action in actions
    )
    if requires_conclusion_change and not patch.conclusion:
        raise ValueError("revision actions require a narrowed conclusion")
    if (requires_conclusion_change or patch.risk_level is not None) and not patch.decision_summary:
        raise ValueError("revision actions require an updated decision summary")

    proposed_text = "\n".join([patch.conclusion or ""] + [claim.text for claim in proposed_claims])
    original_titles = set(re.findall(r"《([^》]+)》", result.conclusion))
    allowed_text = "\n".join(hit.title + "\n" + hit.text for hit in evidence_hits)
    if patch.decision_summary:
        _validate_decision_summary(
            patch.decision_summary,
            supported_text="\n".join([
                proposed_text,
                json.dumps(result.review_facts.model_dump(), ensure_ascii=False),
                allowed_text,
            ]),
        )
    introduced_titles = set(re.findall(r"《([^》]+)》", proposed_text)) - original_titles
    unavailable_titles = [title for title in introduced_titles if title not in allowed_text]
    if unavailable_titles:
        raise ValueError(f"revision introduced unavailable legal sources: {unavailable_titles}")
    return patch


def _normalize_revision_patch(
    patch: ReviewResultPatch,
    *,
    result: ReviewResult,
    actions: list[RevisionAction],
) -> ReviewResultPatch:
    """Compile common LLM patch-shape mistakes into one safe deterministic delta."""

    operations = {action.operation for action in actions}
    narrow_indexes = [
        action.claim_index
        for action in actions
        if action.operation == "narrow_claim" and action.claim_index is not None
    ]
    explicit_remove_indexes = {
        action.claim_index
        for action in actions
        if action.operation == "remove_claim" and action.claim_index is not None
    }

    replacements = list(patch.replace_claims)
    additions = list(patch.add_claims)
    if "add_supported_claim" not in operations and additions:
        if len(additions) == 1 and len(narrow_indexes) == 1:
            replacements.append(
                ClaimReplacement(
                    claim_index=narrow_indexes[0],
                    claim=additions[0],
                )
            )
        additions = []

    unique_replacements: dict[int, ClaimReplacement] = {}
    for replacement in replacements:
        unique_replacements.setdefault(replacement.claim_index, replacement)
    for index in explicit_remove_indexes:
        unique_replacements.pop(index, None)

    replacement_indexes = set(unique_replacements)
    remove_indexes = sorted(
        {
            index
            for index in patch.remove_claim_indexes
            if index not in replacement_indexes or index in explicit_remove_indexes
        }
    )
    if "remove_claim" not in operations and "abstain" not in operations:
        remove_indexes = []
    risk_level = patch.risk_level
    if (
        risk_level == "insufficient_evidence"
        and result.risk_level != "insufficient_evidence"
        and "abstain" not in operations
    ):
        risk_level = None

    return patch.model_copy(
        update={
            "risk_level": risk_level,
            "remove_claim_indexes": remove_indexes,
            "replace_claims": list(unique_replacements.values()),
            "add_claims": additions,
        }
    )


def apply_review_result_patch(
    *,
    result: ReviewResult,
    patch: ReviewResultPatch,
    evidence_hits: list[RetrievalHit],
    chunks_by_id: dict[str, Chunk] | None = None,
) -> ReviewResult:
    """Apply a validated patch while preserving every untouched result field."""

    replacements = {
        replacement.claim_index: replacement.claim for replacement in patch.replace_claims
    }
    removed = set(patch.remove_claim_indexes)
    claims = [
        replacements.get(index, claim)
        for index, claim in enumerate(result.claims)
        if index not in removed
    ]
    claims.extend(patch.add_claims)
    risk_level = patch.risk_level or result.risk_level
    if risk_level == "insufficient_evidence":
        claims = []
    elif claims:
        claims = validate_grounded_claims(claims, evidence_hits)

    conclusion = (patch.conclusion or result.conclusion).rstrip()
    decision_summary = patch.decision_summary or result.decision_summary

    def merged(existing: list[str], additions: list[str]) -> list[str]:
        return list(dict.fromkeys([*existing, *additions]))

    citation_groups, _violations = group_citations(evidence_hits, result.review_facts, chunks_by_id)
    claims = attach_citation_refs(claims, citation_groups)
    citation_ref_by_chunk_id = {
        citation.chunk_id: citation.citation_ref
        for group in citation_groups
        for citation in group.citations
        if citation.citation_ref
    }
    conclusion = inject_citation_markers(
        _remove_citation_markers(conclusion),
        claims,
        evidence_hits,
        citation_ref_by_chunk_id,
    )
    citations = [citation for group in citation_groups for citation in group.citations]

    return result.model_copy(
        update={
            "risk_level": risk_level,
            "decision_summary": decision_summary,
            "conclusion": conclusion,
            "claims": claims,
            "missing_information": merged(
                result.missing_information, patch.append_missing_information
            ),
            "recommended_actions": merged(
                result.recommended_actions, patch.append_recommended_actions
            ),
            "risk_boundaries": merged(result.risk_boundaries, patch.append_risk_boundaries),
            "citations": citations,
            "applicable_evidence": citation_groups,
        }
    )


def _apply_revision_patch_or_fail(
    *,
    result: ReviewResult,
    patch: ReviewResultPatch,
    evidence_hits: list[RetrievalHit],
    chunks_by_id: dict[str, Chunk] | None,
) -> ReviewResult:
    """Convert patch-application invariant failures into a degradable node failure."""

    try:
        return apply_review_result_patch(
            result=result,
            patch=patch,
            evidence_hits=evidence_hits,
            chunks_by_id=chunks_by_id,
        )
    except ValueError as exc:
        raise ReviewWorkflowFailed(
            failed_node="compliance_reviewer_revision",
            reason="revision_patch_application_failed",
            message=str(exc),
            attempts=0,
            trace_id=result.trace_id,
        ) from exc


def revise_review_result_with_deepseek(
    *,
    result: ReviewResult,
    actions: list[RevisionAction],
    evidence_hits: list[RetrievalHit],
    chunks_by_id: dict[str, Chunk] | None = None,
    issue_plan: IssuePlan | None = None,
    evidence_dossiers: list[EvidenceDossier] | None = None,
    client: OpenAICompatibleClient | None = None,
    max_retries: int | None = None,
) -> ReviewResult:
    """Generate and apply one evidence-constrained delta to a valid result."""

    if result.risk_level == "insufficient_evidence":
        gaps = [
            action.reason
            for action in actions
            if action.operation in {"mark_evidence_gap", "abstain", "narrow_claim"}
        ]
        return result.model_copy(
            update={
                "claims": [],
                "missing_information": list(dict.fromkeys([*result.missing_information, *gaps])),
                "risk_boundaries": list(dict.fromkeys([*result.risk_boundaries, *gaps])),
            }
        )

    deterministic_actions = [
        action
        for action in actions
        if action.operation in {"remove_claim", "mark_evidence_gap", "abstain"}
    ]
    language_actions = [
        action
        for action in actions
        if action.operation in {"narrow_claim", "add_supported_claim", "change_risk_boundary"}
    ]
    if deterministic_actions:
        remove_indexes = sorted(
            {
                action.claim_index
                for action in deterministic_actions
                if action.operation == "remove_claim" and action.claim_index is not None
            }
        )
        gaps = [
            action.reason
            for action in deterministic_actions
            if action.operation in {"mark_evidence_gap", "abstain"}
        ]
        abstain = any(action.operation == "abstain" for action in deterministic_actions)
        conclusion = None
        if remove_indexes:
            conclusion = "已移除缺乏当前证据支持的结论，其余判断仅在现有证据边界内成立。"
        if abstain:
            conclusion = "当前材料与可引用证据不足以支持实体合规判断。"
            remove_indexes = list(range(len(result.claims)))
        deterministic_patch = ReviewResultPatch(
            risk_level="insufficient_evidence" if abstain else None,
            conclusion=conclusion,
            remove_claim_indexes=remove_indexes,
            append_missing_information=gaps,
            append_risk_boundaries=gaps,
        )
        result = _apply_revision_patch_or_fail(
            result=result,
            patch=deterministic_patch,
            evidence_hits=evidence_hits,
            chunks_by_id=chunks_by_id,
        )
        if not language_actions or result.risk_level == "insufficient_evidence":
            return result
        actions = language_actions

    if client is None:
        client = OpenAICompatibleClient(require_llm_config())
    node = StructuredLLMNode(
        node_name="compliance_reviewer_revision",
        output_model=ReviewResultPatch,
        client=client,
        max_retries=min(max_retries if max_retries is not None else 1, 1),
        trace_id=result.trace_id,
    )
    patch = node.run(
        build_revision_patch_messages(
            result=result,
            actions=actions,
            evidence_hits=evidence_hits,
            issue_plan=issue_plan,
            evidence_dossiers=evidence_dossiers,
        ),
        post_validate=lambda candidate: _validate_revision_patch(
            _normalize_revision_patch(candidate, result=result, actions=actions),
            result=result,
            actions=actions,
            evidence_hits=evidence_hits,
        ),
        post_validation_reason="revision_patch_validation_failed",
    )
    return _apply_revision_patch_or_fail(
        result=result,
        patch=patch,
        evidence_hits=evidence_hits,
        chunks_by_id=chunks_by_id,
    )
