"""Deterministic national-path rules for outbound data compliance."""

from __future__ import annotations

from law_agent.review.rules.models import (
    CandidatePath,
    ComplianceDecision,
    ComplianceFacts,
    CompliancePathCode,
    MissingFact,
    OfficialBasis,
    RuleHit,
)

RULE_VERSION = "national-cross-border-2024.03-v1"
_RULES_URL = "https://www.cac.gov.cn/2024-03/22/c_1712776611775634.htm"

_BASES = {
    "scope": OfficialBasis(
        basis_id="cross_border_rules_2024_art_2",
        title="促进和规范数据跨境流动规定",
        article="第二条",
        issuing_body="国家互联网信息办公室",
        source_url=_RULES_URL,
    ),
    "exemptions": OfficialBasis(
        basis_id="cross_border_rules_2024_art_4_to_6",
        title="促进和规范数据跨境流动规定",
        article="第四条至第六条",
        issuing_body="国家互联网信息办公室",
        source_url=_RULES_URL,
    ),
    "security": OfficialBasis(
        basis_id="cross_border_rules_2024_art_7",
        title="促进和规范数据跨境流动规定",
        article="第七条",
        issuing_body="国家互联网信息办公室",
        source_url=_RULES_URL,
    ),
    "standard_or_certification": OfficialBasis(
        basis_id="cross_border_rules_2024_art_8",
        title="促进和规范数据跨境流动规定",
        article="第八条",
        issuing_body="国家互联网信息办公室",
        source_url=_RULES_URL,
    ),
    "special_regimes": OfficialBasis(
        basis_id="cross_border_rules_2024_art_6_and_9",
        title="促进和规范数据跨境流动规定",
        article="第六条、第九条",
        issuing_body="国家互联网信息办公室",
        source_url=_RULES_URL,
    ),
}

_PATH_LABELS: dict[CompliancePathCode, str] = {
    "not_applicable": "不属于数据出境法定路径判定范围",
    "statutory_exemption": "法定豁免",
    "security_assessment": "数据出境安全评估",
    "standard_contract_or_certification": "个人信息出境标准合同或个人信息保护认证",
    "no_filing_below_threshold": "未达到安全评估、标准合同或认证数量门槛",
}

_SPECIAL_REGIME_LABELS = {
    "free_trade_zone": "自由贸易试验区负面清单",
    "greater_bay_area": "粤港澳大湾区特别安排",
    "industry_specific": "行业特别规则",
}


def _path(code: CompliancePathCode, reason: str, *, possible: bool = False) -> CandidatePath:
    return CandidatePath(
        code=code,
        label=_PATH_LABELS[code],
        confidence="possible" if possible else "determined",
        reason=reason,
    )


def _missing(key: str, reason: str) -> MissingFact:
    return MissingFact(key=key, reason=reason)


def _possible_paths(facts: ComplianceFacts) -> list[CandidatePath]:
    """Return conservative alternatives without pretending to resolve unknown facts."""

    codes: list[CompliancePathCode] = ["security_assessment"]
    if facts.claimed_exemption is not None:
        codes.append("statutory_exemption")
    codes.extend(["standard_contract_or_certification", "no_filing_below_threshold"])
    return [_path(code, "关键事实尚未确认，该路径仍可能适用。", possible=True) for code in codes]


def evaluate_national_path(facts: ComplianceFacts) -> ComplianceDecision:
    """Evaluate the nationwide statutory path without an LLM or inferred facts."""

    hits: list[RuleHit] = []
    bases: dict[str, OfficialBasis] = {}
    missing: list[MissingFact] = []

    def hit(rule_id: str, summary: str, *basis_keys: str) -> None:
        selected = [_BASES[key] for key in basis_keys]
        hits.append(
            RuleHit(rule_id=rule_id, summary=summary, basis_ids=[x.basis_id for x in selected])
        )
        bases.update((item.basis_id, item) for item in selected)

    manual_reasons = [
        f"识别到{_SPECIAL_REGIME_LABELS[regime]}，仅标记后交由 RAG 检索并由人工确认。"
        for regime in facts.special_regimes
    ]
    if manual_reasons:
        hit(
            "special_regime.requires_human_confirmation",
            "地方、区域或行业特别规则不由全国路径规则自动裁决。",
            "special_regimes",
        )

    def decision(paths: list[CandidatePath]) -> ComplianceDecision:
        return ComplianceDecision(
            status="needs_info" if missing else "determined",
            rule_version=RULE_VERSION,
            candidate_paths=paths,
            needs_info=missing,
            rule_hits=hits,
            official_bases=list(bases.values()),
            requires_rag_human_confirmation=bool(manual_reasons),
            manual_confirmation_reasons=manual_reasons,
        )

    if facts.cross_border_transfer is None:
        missing.append(_missing("cross_border_transfer", "需确认是否存在向境外提供数据的活动。"))
        return decision(_possible_paths(facts))
    if not facts.cross_border_transfer:
        hit("scope.no_cross_border_transfer", "不存在数据出境，不进入出境路径判定。", "scope")
        return decision([_path("not_applicable", "已确认不存在向境外提供数据。")])

    if facts.important_data is None:
        missing.append(_missing("important_data", "重要数据出境将直接触发安全评估。"))
    elif facts.important_data:
        hit(
            "security_assessment.important_data", "向境外提供重要数据，应申报安全评估。", "security"
        )
        return decision([_path("security_assessment", "已确认向境外提供重要数据。")])

    if facts.claimed_exemption is not None:
        if facts.exemption_facts_confirmed is None:
            missing.append(
                _missing(
                    "exemption_facts_confirmed",
                    "需逐项确认所主张法定豁免的构成事实。",
                )
            )
        elif facts.exemption_facts_confirmed and facts.important_data is False:
            hit("statutory_exemption.confirmed", "所主张的法定豁免事实已确认。", "exemptions")
            return decision([_path("statutory_exemption", "法定豁免构成事实已经确认。")])

    if facts.contains_personal_information is None:
        missing.append(
            _missing("contains_personal_information", "需确认出境数据是否包含个人信息。")
        )
    elif not facts.contains_personal_information and facts.important_data is False:
        hit(
            "threshold.no_personal_or_important_data",
            "已确认不含个人信息且不属于重要数据，不适用三项出境机制。",
            "scope",
        )
        return decision(
            [_path("no_filing_below_threshold", "出境数据不含个人信息且不属于重要数据。")]
        )

    if facts.contains_personal_information:
        if facts.is_ciio is None:
            missing.append(
                _missing("is_ciio", "关键信息基础设施运营者提供个人信息应申报安全评估。")
            )
        elif facts.is_ciio:
            hit(
                "security_assessment.ciio_personal_information",
                "CIIO 向境外提供个人信息。",
                "security",
            )
            return decision(
                [_path("security_assessment", "主体已确认为 CIIO，且出境数据含个人信息。")]
            )

        if facts.contains_sensitive_personal_information is None:
            missing.append(
                _missing(
                    "contains_sensitive_personal_information",
                    "敏感个人信息适用独立的万人数量门槛。",
                )
            )
        if facts.cumulative_personal_information_subjects is None:
            missing.append(
                _missing(
                    "cumulative_personal_information_subjects",
                    "需确认当年累计出境的非敏感个人信息主体人数。",
                )
            )
        if (
            facts.contains_sensitive_personal_information
            and facts.cumulative_sensitive_personal_information_subjects is None
        ):
            missing.append(
                _missing(
                    "cumulative_sensitive_personal_information_subjects",
                    "需确认当年累计出境的敏感个人信息主体人数。",
                )
            )

    if missing:
        hit(
            "decision.blocked_by_unknown_facts",
            "关键事实未知，不作推定并暂停确定性判定。",
            "security",
        )
        return decision(_possible_paths(facts))

    ordinary_count = facts.cumulative_personal_information_subjects or 0
    sensitive_count = facts.cumulative_sensitive_personal_information_subjects or 0
    if ordinary_count >= 1_000_000 or sensitive_count >= 10_000:
        hit(
            "security_assessment.volume_threshold",
            "当年累计个人信息数量达到安全评估门槛。",
            "security",
        )
        return decision([_path("security_assessment", "累计出境人数达到第七条门槛。")])
    if ordinary_count >= 100_000 or sensitive_count > 0:
        hit(
            "standard_or_certification.volume_threshold",
            "未达到安全评估门槛，但达到标准合同或认证适用区间。",
            "standard_or_certification",
        )
        return decision(
            [
                _path(
                    "standard_contract_or_certification",
                    "累计出境人数处于第八条规定区间。",
                )
            ]
        )

    hit(
        "threshold.below_filing_threshold",
        "非 CIIO 当年累计出境个人信息低于十万人且不含敏感个人信息。",
        "exemptions",
    )
    return decision([_path("no_filing_below_threshold", "累计人数低于三项出境机制的适用门槛。")])
