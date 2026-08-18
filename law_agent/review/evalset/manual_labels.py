"""Manually curated must-have / optional labels for all 82 evaluation scenarios.

Each entry maps case_id -> (must_have_sources, optional_supporting_sources).

Labeling principles:
- must_have: sources that directly answer the core legal question; without them
  the answer is fundamentally incomplete or wrong.
- optional: sources that provide additional context, implementation detail,
  or auxiliary interpretation; they enhance but are not strictly required.

No automatic rules (can_cite_clause, citation_role, etc.) are used.
Every label is manually decided based on the question's intent.
"""

from __future__ import annotations

# type: dict[str, tuple[list[str], list[str]]]
MANUAL_LABELS: dict[str, tuple[list[str], list[str]]] = {

    # ------------------------------------------------------------------
    # Cross-border data export assessment (base)
    # ------------------------------------------------------------------
    "eval_cross_border_001": (
        [
            "cac_data_export_security_assessment_measures_2022",
            "cac_cross_border_data_flow_rules_2024",
            "cac_data_export_security_assessment_filing_guide_v3_2025",
        ],
        ["cac_data_export_assessment_qna_2022"],
    ),
    "eval_cross_border_002": (
        [
            "cac_data_export_security_assessment_measures_2022",
            "cac_data_export_security_assessment_filing_guide_v3_2025",
        ],
        [],
    ),
    "eval_cross_border_003": (
        [
            "cac_data_export_security_assessment_measures_2022",
            "cac_cross_border_data_flow_rules_2024",
        ],
        ["cac_data_export_assessment_qna_2022"],
    ),
    "eval_cross_border_004": (
        [
            "cac_data_export_security_assessment_measures_2022",
            "cac_data_export_security_assessment_filing_guide_v3_2025",
        ],
        [],
    ),
    "eval_cross_border_005": (
        [
            "cac_data_export_security_assessment_measures_2022",
            "cac_data_export_security_assessment_filing_guide_v3_2025",
        ],
        [],
    ),

    # ------------------------------------------------------------------
    # Standard contract (base)
    # ------------------------------------------------------------------
    "eval_standard_contract_001": (
        ["cac_personal_info_export_standard_contract_measures_2023"],
        ["cac_personal_info_export_standard_contract_filing_guide_v2_2024"],
    ),
    "eval_standard_contract_002": (
        [
            "cac_personal_info_export_standard_contract_filing_guide_v2_2024",
            "cac_personal_info_export_standard_contract_template_2023",
        ],
        [],
    ),
    "eval_standard_contract_003": (
        [
            "cac_personal_info_export_standard_contract_measures_2023",
            "cac_personal_info_export_standard_contract_filing_guide_v2_2024",
        ],
        [],
    ),
    "eval_standard_contract_004": (
        [
            "cac_personal_info_export_standard_contract_template_2023",
            "cac_personal_info_export_standard_contract_measures_2023",
        ],
        [],
    ),

    # ------------------------------------------------------------------
    # Automotive (base)
    # ------------------------------------------------------------------
    "eval_automotive_001": (
        [
            "cac_automotive_data_security_provisions_2021",
            "cac_automotive_data_export_security_guide_2026",
            "cac_data_export_security_assessment_measures_2022",
        ],
        [],
    ),
    "eval_automotive_002": (
        ["cac_automotive_data_security_provisions_2021", "mnr_intelligent_connected_vehicle_geoinformation_security_notice_2022"],
        [],
    ),
    "eval_automotive_003": (
        ["mnr_intelligent_connected_vehicle_geoinformation_security_notice_2022", "cac_automotive_data_security_provisions_2021"],
        [],
    ),
    "eval_automotive_004": (
        [
            "chongqing_free_trade_zone_data_export_negative_list_2025",
            "cac_automotive_data_security_provisions_2021",
            "cac_cross_border_data_flow_rules_2024",
        ],
        [],
    ),

    # ------------------------------------------------------------------
    # Regional negative list (base)
    # ------------------------------------------------------------------
    "eval_shanghai_001": (
        ["shanghai_free_trade_zone_data_export_negative_list_2024", "cac_cross_border_data_flow_rules_2024"],
        ["shanghai_free_trade_zone_data_export_negative_list_qna_2024"],
    ),
    "eval_tianjin_001": (
        ["tianjin_free_trade_zone_data_export_negative_list_2024", "cac_cross_border_data_flow_rules_2024"],
        [],
    ),
    "eval_hainan_001": (
        ["hainan_free_trade_port_data_export_negative_list_2024", "cac_cross_border_data_flow_rules_2024"],
        [],
    ),
    "eval_beijing_001": (
        ["beijing_free_trade_zone_data_export_negative_list_2025", "cac_cross_border_data_flow_rules_2024"],
        [],
    ),
    "eval_zhejiang_001": (
        ["zhejiang_free_trade_zone_data_export_negative_list_2024", "cac_cross_border_data_flow_rules_2024"],
        [],
    ),

    # ------------------------------------------------------------------
    # Sensitive personal information (base)
    # ------------------------------------------------------------------
    "eval_sensitive_001": (
        [
            "flk_npc_ff8081817b6472a3017b656cc2040044",
            "tc260_sensitive_pip_identification_guide_2024",
        ],
        ["tc260_sensitive_pip_processing_requirements_2025"],
    ),
    "eval_sensitive_002": (
        [
            "flk_npc_ff8081817b6472a3017b656cc2040044",
            "tc260_sensitive_pip_identification_guide_2024",
        ],
        ["tc260_sensitive_pip_processing_requirements_2025"],
    ),

    # ------------------------------------------------------------------
    # Data classification (base)
    # ------------------------------------------------------------------
    "eval_classification_001": (
        [
            "flk_npc_ff80818179f5e0800179f885c7e70392",
            "tc260_gbt_43697_2024_data_classification_rules",
        ],
        ["tc260_network_data_classification_guide_2021"],
    ),
    "eval_financial_001": (
        ["cac_financial_information_service_data_classification_guide_2026"],
        ["tc260_gbt_43697_2024_data_classification_rules"],
    ),

    # ------------------------------------------------------------------
    # Abstention (base) — no expected sources
    # ------------------------------------------------------------------
    "eval_abstain_001": ([], []),
    "eval_abstain_002": ([], []),

    # ------------------------------------------------------------------
    # Cross-border (extra)
    # ------------------------------------------------------------------
    "eval_cross_border_006": (
        [
            "cac_data_export_security_assessment_measures_2022",
            "cac_cross_border_data_flow_rules_2024",
            "cac_data_export_security_assessment_filing_guide_v3_2025",
        ],
        [],
    ),
    "eval_cross_border_007": (
        [
            "cac_data_export_security_assessment_measures_2022",
            "cac_cross_border_data_flow_rules_2024",
            "flk_npc_ff8081817b63b935017b7bcc49877e0b",
        ],
        [],
    ),
    "eval_cross_border_008": (
        [
            "cac_data_export_security_assessment_measures_2022",
            "cac_data_export_security_assessment_filing_guide_v3_2025",
            "cac_cross_border_data_flow_rules_2024",
        ],
        [],
    ),
    "eval_cross_border_009": (
        [
            "cac_data_export_security_assessment_measures_2022",
            "cac_data_export_security_assessment_filing_guide_v3_2025",
        ],
        ["cac_personal_info_export_standard_contract_template_2023"],
    ),
    "eval_cross_border_010": (
        [
            "cac_data_export_security_assessment_filing_guide_v3_2025",
            "cac_data_export_security_assessment_measures_2022",
        ],
        [],
    ),
    "eval_cross_border_011": (
        [
            "cac_cross_border_data_flow_rules_2024",
            "cac_personal_info_export_standard_contract_measures_2023",
        ],
        [],
    ),
    "eval_cross_border_012": (
        ["cac_cross_border_data_flow_rules_2024"],
        ["cac_data_export_policy_qna_2025_04"],
    ),
    "eval_cross_border_013": (
        [
            "cac_data_export_security_assessment_measures_2022",
            "cac_cross_border_data_flow_rules_2024",
        ],
        ["cac_data_export_assessment_qna_2022"],
    ),
    "eval_cross_border_014": (
        [
            "cac_cross_border_data_flow_rules_2024",
            "cac_data_export_security_assessment_measures_2022",
        ],
        [],
    ),

    # ------------------------------------------------------------------
    # Standard contract (extra)
    # ------------------------------------------------------------------
    "eval_standard_contract_005": (
        ["cac_personal_info_export_standard_contract_measures_2023"],
        ["cac_personal_info_export_standard_contract_filing_guide_v2_2024"],
    ),
    "eval_standard_contract_006": (
        ["cac_personal_info_export_standard_contract_measures_2023"],
        ["cac_personal_info_export_standard_contract_filing_guide_v2_2024"],
    ),
    "eval_standard_contract_007": (
        ["cac_personal_info_export_standard_contract_measures_2023"],
        ["cac_personal_info_export_standard_contract_filing_guide_v2_2024"],
    ),
    "eval_standard_contract_008": (
        [
            "cac_personal_info_export_standard_contract_measures_2023",
            "cac_personal_info_export_certification_measures_2025",
        ],
        ["cac_cross_border_flow_rules_qna_2024"],
    ),
    "eval_standard_contract_009": (
        [
            "cac_personal_info_export_standard_contract_template_2023",
            "cac_personal_info_export_standard_contract_measures_2023",
        ],
        [],
    ),
    "eval_standard_contract_010": (
        [
            "cac_personal_info_export_standard_contract_template_2023",
            "cac_personal_info_export_standard_contract_measures_2023",
        ],
        [],
    ),

    # ------------------------------------------------------------------
    # Certification (extra)
    # ------------------------------------------------------------------
    "eval_certification_001": (
        [
            "cac_personal_info_export_certification_measures_2025",
            "cac_personal_info_protection_certification_rules_2022",
        ],
        ["tc260_cross_border_certification_spec_2022"],
    ),
    "eval_certification_002": (
        [
            "cac_personal_info_export_certification_measures_2025",
            "cac_personal_info_protection_certification_rules_2022",
        ],
        ["tc260_cross_border_certification_spec_2022"],
    ),
    "eval_certification_003": (
        [
            "cac_personal_info_export_certification_measures_2025",
            "tc260_gba_cross_border_pip_protection_2024",
        ],
        ["tc260_cross_border_certification_spec_2022"],
    ),
    "eval_certification_004": (
        [
            "cac_personal_info_export_certification_measures_2025",
            "flk_npc_ff8081817b6472a3017b656cc2040044",
        ],
        ["tc260_cross_border_certification_spec_2022"],
    ),
    "eval_certification_005": (
        [
            "cac_personal_info_export_certification_measures_2025",
            "cac_personal_info_export_standard_contract_measures_2023",
        ],
        ["cac_cross_border_flow_rules_qna_2024"],
    ),
    "eval_certification_006": (
        ["cac_personal_info_export_certification_measures_2025"],
        ["tc260_cross_border_certification_spec_2022"],
    ),

    # ------------------------------------------------------------------
    # Automotive (extra)
    # ------------------------------------------------------------------
    "eval_automotive_005": (
        ["cac_automotive_data_security_provisions_2021", "mnr_intelligent_connected_vehicle_geoinformation_security_notice_2022"],
        [],
    ),
    "eval_automotive_006": (
        [
            "cac_automotive_data_export_security_guide_2026",
            "cac_automotive_data_security_provisions_2021",
            "cac_data_export_security_assessment_measures_2022",
        ],
        [],
    ),
    "eval_automotive_007": (
        [
            "cac_automotive_data_security_provisions_2021",
            "flk_npc_ff8081817b6472a3017b656cc2040044",
        ],
        ["tc260_sensitive_pip_identification_guide_2024"],
    ),
    "eval_automotive_008": (
        ["mnr_intelligent_connected_vehicle_geoinformation_security_notice_2022", "cac_automotive_data_security_provisions_2021"],
        [],
    ),
    "eval_automotive_009": (
        ["cac_automotive_data_security_provisions_2021", "cac_automotive_data_export_security_guide_2026"],
        [],
    ),
    "eval_automotive_010": (
        [
            "cac_automotive_data_security_provisions_2021",
            "cac_automotive_data_export_security_guide_2026",
            "cac_data_export_security_assessment_filing_guide_v3_2025",
        ],
        [],
    ),

    # ------------------------------------------------------------------
    # Financial (extra)
    # ------------------------------------------------------------------
    "eval_financial_002": (
        ["cac_financial_information_service_data_classification_guide_2026"],
        [
            "tc260_gbt_43697_2024_data_classification_rules",
            "tc260_network_data_classification_guide_2021",
        ],
    ),
    "eval_financial_003": (
        [
            "cac_financial_information_service_data_classification_guide_2026",
            "flk_npc_ff8081817b6472a3017b656cc2040044",
        ],
        ["tc260_sensitive_pip_processing_requirements_2025"],
    ),
    "eval_financial_004": (
        [
            "cac_financial_information_service_data_classification_guide_2026",
            "cac_data_export_security_assessment_measures_2022",
            "cac_cross_border_data_flow_rules_2024",
        ],
        [],
    ),
    "eval_financial_005": (
        [
            "flk_npc_ff80818179f5e0800179f885c7e70392",
            "cac_financial_information_service_data_classification_guide_2026",
        ],
        ["tc260_gbt_43697_2024_data_classification_rules"],
    ),

    # ------------------------------------------------------------------
    # Regional negative lists (extra)
    # ------------------------------------------------------------------
    "eval_fujian_001": (
        ["fujian_free_trade_zone_data_export_negative_list_2025", "cac_cross_border_data_flow_rules_2024"],
        [],
    ),
    "eval_guangdong_001": (
        ["guangdong_free_trade_zone_data_export_negative_list_2025", "cac_cross_border_data_flow_rules_2024"],
        [],
    ),
    "eval_jiangsu_001": (
        ["jiangsu_free_trade_zone_data_export_negative_list_2025", "cac_cross_border_data_flow_rules_2024"],
        [],
    ),
    "eval_guangxi_001": (
        ["guangxi_free_trade_zone_data_export_negative_list_2025", "cac_cross_border_data_flow_rules_2024"],
        [],
    ),
    "eval_shenzhen_001": (
        [
            "shenzhen_data_regulation_2021",
            "flk_npc_ff80818179f5e0800179f885c7e70392",
        ],
        [],
    ),
    "eval_regional_compare_001": (
        [
            "zhejiang_free_trade_zone_data_export_negative_list_2024",
            "fujian_free_trade_zone_data_export_negative_list_2025",
            "cac_cross_border_data_flow_rules_2024",
        ],
        [],
    ),
    "eval_regional_compare_002": (
        [
            "guangdong_free_trade_zone_data_export_negative_list_2025",
            "hainan_free_trade_port_data_export_negative_list_2024",
            "cac_cross_border_data_flow_rules_2024",
        ],
        [],
    ),
    "eval_regional_boundary_001": (
        ["shanghai_free_trade_zone_data_export_negative_list_2024", "cac_cross_border_data_flow_rules_2024"],
        [],
    ),
    "eval_regional_boundary_002": (
        [
            "cac_cross_border_data_flow_rules_2024",
            "cac_data_export_security_assessment_measures_2022",
        ],
        [],
    ),

    # ------------------------------------------------------------------
    # TC260 / GB/T standards (extra)
    # ------------------------------------------------------------------
    "eval_tc260_pip_001": (
        [
            "flk_npc_ff8081817b6472a3017b656cc2040044",
            "tc260_gbt_35273_2020_pip_security_spec",
        ],
        [],
    ),
    "eval_tc260_sensitive_001": (
        [
            "tc260_sensitive_pip_identification_guide_2024",
            "tc260_sensitive_pip_processing_requirements_2025",
            "flk_npc_ff8081817b6472a3017b656cc2040044",
        ],
        [],
    ),
    "eval_tc260_classification_001": (
        [
            "tc260_gbt_43697_2024_data_classification_rules",
            "tc260_network_data_classification_guide_2021",
            "flk_npc_ff80818179f5e0800179f885c7e70392",
        ],
        [],
    ),
    "eval_tc260_cross_border_001": (
        [
            "tc260_cross_border_certification_spec_2022",
            "cac_personal_info_export_certification_measures_2025",
        ],
        [],
    ),
    "eval_tc260_gba_001": (
        [
            "tc260_gba_cross_border_pip_protection_2024",
            "cac_personal_info_export_certification_measures_2025",
        ],
        [],
    ),
    "eval_tc260_security_controls_001": (
        [
            "flk_npc_ff8081817b6472a3017b656cc2040044",
            "tc260_gbt_35273_2020_pip_security_spec",
        ],
        ["tc260_sensitive_pip_processing_requirements_2025"],
    ),

    # ------------------------------------------------------------------
    # Q&A and interpretation materials (extra)
    # ------------------------------------------------------------------
    "eval_qna_001": (
        [
            "cac_data_export_assessment_qna_2022",
            "cac_data_export_security_assessment_measures_2022",
        ],
        [],
    ),
    "eval_qna_002": (
        [
            "cac_cross_border_flow_rules_qna_2024",
            "cac_cross_border_data_flow_rules_2024",
        ],
        [],
    ),
    "eval_qna_003": (
        [
            "cac_data_export_policy_qna_2025_04",
            "cac_cross_border_data_flow_rules_2024",
        ],
        [],
    ),
    "eval_qna_004": (
        [
            "cac_network_data_security_regulation_qna_2024",
            "flk_npc_ff808181927f0e7b0192949a1da4355d",
        ],
        [],
    ),

    # ------------------------------------------------------------------
    # Abstention / out-of-corpus / boundary / conflict (extra)
    # ------------------------------------------------------------------
    "eval_abstain_003": ([], []),
    "eval_abstain_004": ([], []),
    "eval_out_of_corpus_001": ([], []),
    "eval_out_of_corpus_002": ([], []),

    "eval_boundary_001": (
        [
            "flk_npc_ff8081817b6472a3017b656cc2040044",
            "cac_personal_info_export_standard_contract_measures_2023",
        ],
        [],
    ),
    "eval_boundary_002": (
        [
            "flk_npc_ff8081817b6472a3017b656cc2040044",
            "tc260_sensitive_pip_identification_guide_2024",
        ],
        ["tc260_sensitive_pip_processing_requirements_2025"],
    ),
    "eval_conflict_001": (
        [
            "guangdong_free_trade_zone_data_export_negative_list_2025",
            "cac_cross_border_data_flow_rules_2024",
            "cac_data_export_security_assessment_measures_2022",
        ],
        [],
    ),
    "eval_conflict_002": (
        [
            "cac_data_export_security_assessment_measures_2022",
            "cac_personal_info_export_standard_contract_measures_2023",
        ],
        ["cac_cross_border_flow_rules_qna_2024"],
    ),
}
