"""Assemble report data from persisted case, review, and rule records.

The API module owns transport and workflow transitions.  This module owns the
translation from those persisted records into the stable report data model,
including citation selection and legal-source ordering.
"""

from __future__ import annotations

import html
import re
from typing import Any

from law_agent.review.reports import AIReviewSummary, LegalSource, RemediationDetail


def _compact_text(value: Any, *, limit: int) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", "", str(value or "")))
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}…"


def _narrative_text(value: Any, *, limit: int | None = 1200) -> str:
    text = html.unescape(re.sub(r"<[^>]+>", "", str(value or "")))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    if limit is None or len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}…"


def _text_items(values: Any, *, limit: int = 320) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    items: list[str] = []
    for value in values:
        text = _compact_text(value, limit=limit)
        if text and text not in items:
            items.append(text)
    return tuple(items)


def build_ai_review(case: dict[str, Any]) -> AIReviewSummary | None:
    response = case.get("response")
    if not isinstance(response, dict):
        return None
    result = response.get("review_result")
    if not isinstance(result, dict):
        return None
    facts = result.get("review_facts") or response.get("review_facts") or {}
    if not isinstance(facts, dict):
        facts = {}
    return AIReviewSummary(
        risk_level=str(result.get("risk_level") or ""),
        decision_summary=_compact_text(result.get("decision_summary"), limit=240),
        # The decision report is an audit artifact. Keep the complete review;
        # pagination belongs to the renderer and must not be replaced by a
        # silent character limit here.
        conclusion=_narrative_text(result.get("conclusion"), limit=None),
        missing_information=_text_items(result.get("missing_information")),
        recommended_actions=_text_items(result.get("recommended_actions")),
        risk_boundaries=_text_items(result.get("risk_boundaries")),
        business_activity=_compact_text(facts.get("business_activity"), limit=220),
        overseas_recipient=_compact_text(facts.get("overseas_recipient"), limit=180),
        processing_purpose=_compact_text(facts.get("processing_purpose"), limit=220),
        data_types=_text_items(facts.get("data_types"), limit=100),
    )


def _article_order(value: str) -> tuple[int, int, str]:
    match = re.search(r"第([一二三四五六七八九十百千万零〇]+)条", value)
    if not match:
        return (1, 9999, value)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    section = 0
    number = 0
    for char in match.group(1):
        if char in digits:
            number = digits[char]
        elif char == "十":
            section += (number or 1) * 10
            number = 0
        elif char == "百":
            section += (number or 1) * 100
            number = 0
        elif char == "千":
            section += (number or 1) * 1000
            number = 0
        elif char == "万":
            section = (section + number) * 10000
            number = 0
    return (0, section + number, value)


def _title_order(value: str) -> int:
    if "促进和规范数据跨境流动规定" in value:
        return 0
    if "个人信息出境标准合同办法" in value:
        return 1
    if "数据出境安全评估办法" in value:
        return 2
    if "个人信息保护法" in value:
        return 3
    if "个人信息出境标准合同" in value:
        return 4
    return 9


def build_legal_sources(
    case: dict[str, Any], determination: dict[str, Any], selected_path: str
) -> tuple[LegalSource, ...]:
    response = case.get("response")
    if not isinstance(response, dict):
        response = {}
    result = response.get("review_result")
    if not isinstance(result, dict):
        result = {}
    citations = result.get("citations") or []
    if not isinstance(citations, list) or not citations:
        citations = [
            citation
            for group in response.get("citation_groups") or []
            if isinstance(group, dict)
            for citation in group.get("citations") or []
            if isinstance(citation, dict)
        ]

    applications: dict[str, list[str]] = {}
    claim_order: dict[str, int] = {}
    for index, claim in enumerate(result.get("claims") or []):
        if not isinstance(claim, dict):
            continue
        claim_text = _compact_text(claim.get("text"), limit=360)
        for citation_ref in claim.get("supporting_citation_refs") or []:
            ref = str(citation_ref)
            claim_order.setdefault(ref, index)
            if claim_text and claim_text not in applications.setdefault(ref, []):
                applications[ref].append(claim_text)

    role_rank = {"legal_basis": 0, "conditional_basis": 1, "implementation_reference": 2}
    candidates: list[tuple[LegalSource, int, int, int]] = []
    seen: set[tuple[str, str]] = set()
    for position, citation in enumerate(citations):
        if not isinstance(citation, dict):
            continue
        title = _compact_text(citation.get("title"), limit=120)
        if not title:
            continue
        role = str(citation.get("usage") or citation.get("citation_role") or "legal_basis")
        if role not in role_rank:
            continue
        article = _compact_text(
            citation.get("article_no") or citation.get("citation_label"), limit=100
        )
        key = (title, article)
        if key in seen:
            continue
        seen.add(key)
        citation_ref = str(citation.get("citation_ref") or "")
        application = "；".join(applications.get(citation_ref, ()))
        if not application and selected_path and "标准合同" in title and "标准合同" in selected_path:
            application = (
                f"本案选择“{selected_path}”，该材料用于支撑该路径下的合同订立、备案或持续履行要求。"
            )
        source = LegalSource(
            title=title,
            locator=str(citation.get("source_url") or citation.get("citation_label") or ""),
            article=str(citation.get("article_no") or ""),
            provision_text=_compact_text(citation.get("full_article_text"), limit=760),
            application=application,
            role=role,
            citation_ref=citation_ref,
        )
        candidates.append(
            (source, _title_order(title), claim_order.get(citation_ref, 99), role_rank[role] * 100 + position)
        )

    if candidates:
        candidates.sort(key=lambda item: (item[1], item[2], _article_order(item[0].article), item[3]))
        grouped: dict[str, list[tuple[LegalSource, int, int, int]]] = {}
        for item in candidates:
            grouped.setdefault(item[0].title, []).append(item)
        selected: list[LegalSource] = []
        for title in sorted(grouped, key=_title_order):
            group = grouped[title]
            supported = [item for item in group if item[2] < 99]
            if supported:
                selected.extend(item[0] for item in supported[:3])
            elif selected_path and "标准合同" in title and "标准合同" in selected_path:
                selected.extend(item[0] for item in group[:2])
            if len(selected) >= 6:
                break
        if len(selected) < 6:
            for source, *_ in candidates:
                if source not in selected:
                    selected.append(source)
                if len(selected) >= 6:
                    break
        selected.sort(key=lambda source: (_title_order(source.title), _article_order(source.article)))
        return tuple(selected[:6])

    fallback: list[LegalSource] = []
    for item in determination.get("official_bases") or []:
        if not isinstance(item, dict):
            continue
        title = _compact_text(item.get("title"), limit=120)
        article = _compact_text(item.get("article"), limit=100)
        if title:
            fallback.append(
                LegalSource(
                    title=title,
                    locator=str(item.get("source_url") or ""),
                    article=article,
                    role="legal_basis",
                )
            )
    fallback.sort(key=lambda source: (_title_order(source.title), _article_order(source.article)))
    return tuple(fallback[:6])


def build_remediation_details(actions: list[dict[str, Any]]) -> tuple[RemediationDetail, ...]:
    priority_labels = {"high": "高优先级", "medium": "中优先级", "low": "低优先级"}
    status_labels = {"open": "待完成", "in_progress": "进行中", "completed": "已完成"}
    details: list[RemediationDetail] = []
    for item in actions:
        title = _compact_text(item.get("title"), limit=180)
        if not title:
            continue
        details.append(
            RemediationDetail(
                title=title,
                description=_compact_text(item.get("description"), limit=180),
                owner_role=_compact_text(item.get("owner_role"), limit=80),
                priority=priority_labels.get(str(item.get("priority") or "").lower(), ""),
                required_before=_compact_text(item.get("due_date"), limit=80),
                status=status_labels.get(str(item.get("status") or "").lower(), "待完成"),
                evidence_expected=_compact_text(item.get("evidence_expected"), limit=180),
            )
        )
    return tuple(details)


def selected_path_for_report(determination: dict[str, Any]) -> str:
    for item in determination.get("candidate_paths") or []:
        if isinstance(item, dict) and item.get("confidence") == "determined":
            return str(item.get("label") or item.get("code") or "")
    return ""


def manual_confirmations_for_report(determination: dict[str, Any]) -> tuple[str, ...]:
    values = determination.get("manual_confirmation_reasons") or determination.get("manual_confirmation_required") or []
    return _text_items(values, limit=240)
