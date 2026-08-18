"""Citation policy for deciding which evidence may support clause-level citations."""

from __future__ import annotations

from typing import Literal

ClauseCitationRole = Literal[
    "primary_legal_basis",
    "conditional_local_basis",
    "conditional_industry_basis",
    "implementation_reference",
    "interpretation_auxiliary",
]

CLAUSE_CITABLE_SOURCE_IDS = {
    "flk_npc_ff8081817b63b935017b7bcc49877e0b",
    "flk_npc_ff808181927f0e7b0192949a1da4355d",
    "flk_npc_ff8081817b6472a3017b656cc2040044",
    "flk_npc_ff80818179f5e0800179f885c7e70392",
    "flk_npc_021e7d7684474107b8f3febbb1c4f8b5",
    "cac_data_export_security_assessment_measures_2022",
    "cac_personal_info_export_standard_contract_measures_2023",
    "cac_cross_border_data_flow_rules_2024",
    "cac_cybersecurity_review_measures_2022",
    "cac_personal_info_protection_certification_rules_2022",
    "cac_personal_info_export_certification_measures_2025",
    "cac_data_export_security_assessment_filing_guide_v3_2025",
}

LOCAL_CONDITIONAL_SOURCE_IDS = {
    "flk_npc_4028abcc61277793016127eca82f2c61",
    "flk_npc_ff808181857bbb76018594676d0f2a7d",
    "flk_npc_ff8081817ddb1774017dea2a41241fc9",
    "flk_npc_ff8081819cf9f6cf019d763ae3b028eb",
    "tianjin_free_trade_zone_data_export_negative_list_2024",
    "hainan_free_trade_port_data_export_negative_list_2024",
    "beijing_free_trade_zone_data_export_negative_list_2025",
    "fujian_free_trade_zone_data_export_negative_list_2025",
    "shenzhen_data_regulation_2021",
    "shanghai_free_trade_zone_data_export_negative_list_2024",
    "guangdong_free_trade_zone_data_export_negative_list_2025",
    "jiangsu_free_trade_zone_data_export_negative_list_2025",
    "chongqing_free_trade_zone_data_export_negative_list_2025",
    "zhejiang_free_trade_zone_data_export_negative_list_2024",
    "guangxi_free_trade_zone_data_export_negative_list_2025",
}

INDUSTRY_CONDITIONAL_SOURCE_IDS = {
    "cac_automotive_data_security_provisions_2021",
    "cac_automotive_data_export_security_guide_2026",
    "mnr_intelligent_connected_vehicle_geoinformation_security_notice_2022",
    "cac_financial_information_service_data_classification_guide_2026",
}

INTERPRETATION_AUXILIARY_SOURCE_IDS = {
    "cac_data_export_assessment_qna_2022",
    "cac_cross_border_flow_rules_qna_2024",
    "cac_network_data_security_regulation_qna_2024",
    "cac_data_export_policy_qna_2025_04",
    "cac_data_export_policy_qna_2026_01",
    "shanghai_free_trade_zone_data_export_negative_list_qna_2024",
}

IMPLEMENTATION_REFERENCE_SOURCE_IDS = {
    "cac_personal_info_export_standard_contract_filing_guide_v2_2024",
    "cac_personal_info_export_standard_contract_template_2023",
}

FRONTEND_DIRECT_REFERENCE_SOURCE_IDS = {
    "flk_npc_ff808181927f0e7b0192949a1da4355d",
    "flk_npc_ff8081817b6472a3017b656cc2040044",
    "flk_npc_ff80818179f5e0800179f885c7e70392",
    "flk_npc_021e7d7684474107b8f3febbb1c4f8b5",
    "cac_data_export_security_assessment_measures_2022",
    "cac_personal_info_export_standard_contract_measures_2023",
    "cac_personal_info_export_standard_contract_filing_guide_v2_2024",
    "cac_personal_info_export_standard_contract_template_2023",
    "cac_data_export_security_assessment_filing_guide_v3_2025",
}


def citation_role_for_source(source_id: str) -> ClauseCitationRole:
    """Return the role this source can play in a legal answer."""

    if source_id in CLAUSE_CITABLE_SOURCE_IDS:
        return "primary_legal_basis"
    if source_id in LOCAL_CONDITIONAL_SOURCE_IDS:
        return "conditional_local_basis"
    if source_id in INDUSTRY_CONDITIONAL_SOURCE_IDS:
        return "conditional_industry_basis"
    if source_id.startswith("tc260_") or source_id in IMPLEMENTATION_REFERENCE_SOURCE_IDS:
        return "implementation_reference"
    if source_id in INTERPRETATION_AUXILIARY_SOURCE_IDS:
        return "interpretation_auxiliary"
    return "interpretation_auxiliary"


def can_cite_clause(source_id: str) -> bool:
    """Source-level citability: True for primary legal basis sources.

    This is the source-level gate. For chunk-level citation decisions
    (whether a specific chunk may be quoted as a concrete clause), use
    :func:`can_cite_clause_chunk` instead, which additionally requires
    the chunk to carry a concrete ``article_no``.
    """

    return citation_role_for_source(source_id) == "primary_legal_basis"


def can_cite_clause_chunk(source_id: str, article_no: str | None) -> bool:
    """Chunk-level citability for concrete clause citations.

    A chunk may be quoted as a specific clause (e.g. "《个人信息保护法》
    第三十九条") only when **both** conditions hold:

    1. The source is a primary legal basis (``can_cite_clause(source_id)``).
    2. The chunk carries a concrete ``article_no`` (e.g. "第三十九条").

    Guide/template/certification-rule chunks that belong to citable
    sources but lack article numbers remain retrievable and appear in the
    evidence panel — they just cannot be inlined as concrete clause
    citations in the conclusion.
    """

    return can_cite_clause(source_id) and bool(article_no)


def default_retrievable_for_source(source_id: str) -> bool:
    """Return whether the source should be in normal retrieval by default."""

    citation_role_for_source(source_id)
    return True


def frontend_direct_reference_for_source(source_id: str) -> bool:
    """Return whether the source should be shown as a fixed frontend reference."""

    return source_id in FRONTEND_DIRECT_REFERENCE_SOURCE_IDS
