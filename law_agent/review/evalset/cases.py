"""Golden-set scenarios for review evaluation (Issue 9).

Base scenario cases cover:
- Cross-border data export assessment (5 cases)
- Standard contract for personal info export (4 cases)
- Smart connected vehicle / automotive (4 cases)
- Regional negative lists and local rules (5 cases)
- Sensitive personal information (2 cases)
- Data classification / security governance (2 cases)
- Abstention cases (2 cases)
"""

from __future__ import annotations

from typing import Literal

from law_agent.review.evalset.cases_full_extra import FULL_EXTRA_SCENARIOS
from law_agent.review.evalset.cases_quick import QUICK_CASE_IDS
from law_agent.review.evalset.schemas import EvalScenario

EvalSuite = Literal["quick", "base", "full"]

BASE_SCENARIOS: list[EvalScenario] = [
    # ------------------------------------------------------------------
    # Cross-border data export assessment
    # ------------------------------------------------------------------
    EvalScenario(
        case_id="eval_cross_border_001",
        question="这种规模的出境要不要先向网信部门申报？",
        material_text=(
            "我们会将手机号和定位信息发送给新加坡服务商用于推荐优化。"
            "目前日均处理约50万用户的数据。"
        ),
        expected_sources=[
            "cac_data_export_security_assessment_measures_2022",
            "cac_data_export_assessment_qna_2022",
            "cac_cross_border_data_flow_rules_2024",
            "cac_data_export_security_assessment_filing_guide_v3_2025",
        ],
        expected_citation_roles=["primary_legal_basis"],
                should_abstain=False,
        must_not_cite_as_clause=[],
        tags=["cross_border", "assessment", "personal_info"],
    ),
    EvalScenario(
        case_id="eval_cross_border_002",
        question="什么情况下必须走网信办申报，而不是只做普通备案？",
        material_text=(
            "公司是大型互联网平台，处理超过1000万用户个人信息，"
            "计划将部分用户数据传输至美国云服务商。"
        ),
        expected_sources=[
            "cac_data_export_security_assessment_measures_2022",
            "cac_data_export_security_assessment_filing_guide_v3_2025",
        ],
        expected_citation_roles=["primary_legal_basis"],
                should_abstain=False,
        tags=["cross_border", "assessment", "large_scale"],
    ),
    EvalScenario(
        case_id="eval_cross_border_003",
        question="评估通过之后，业务继续跑还要盯哪些后续义务？",
        material_text=(
            "我们已通过数据出境安全评估，将用户行为数据传输至日本分析中心。"
            "想知道后续还需要履行哪些义务。"
        ),
        expected_sources=[
            "cac_data_export_security_assessment_measures_2022",
            "cac_cross_border_data_flow_rules_2024",
            "cac_data_export_assessment_qna_2022",
        ],
        expected_citation_roles=["primary_legal_basis"],
                should_abstain=False,
        tags=["cross_border", "post_assessment"],
    ),
    EvalScenario(
        case_id="eval_cross_border_004",
        question="海外 BI 团队要看国内用户画像，是不是一定要走安全评估？",
        material_text=(
            "业务部门计划把订单记录、浏览行为和用户画像同步给美国 BI 团队。"
            "平台累计注册用户约120万人，近期一年预计涉及约12万人个人信息。"
        ),
        expected_sources=[
            "cac_data_export_security_assessment_measures_2022",
            "cac_data_export_security_assessment_filing_guide_v3_2025",
        ],
        expected_citation_roles=["primary_legal_basis"],
                should_abstain=False,
        tags=["cross_border", "assessment", "implicit"],
    ),
    EvalScenario(
        case_id="eval_cross_border_005",
        question="如果境外接收方和处理目的都变了，原来通过的出境安排还能继续用吗？",
        material_text=(
            "公司去年已完成数据出境安全评估。现在拟把接收方从日本分析中心改为新加坡云服务商，"
            "数据用途也从统计分析扩展到个性化推荐。"
        ),
        expected_sources=[
            "cac_data_export_security_assessment_measures_2022",
            "cac_data_export_security_assessment_filing_guide_v3_2025",
        ],
        expected_citation_roles=["primary_legal_basis"],
                should_abstain=False,
        tags=["cross_border", "post_assessment", "change"],
    ),

    # ------------------------------------------------------------------
    # Standard contract for personal info export
    # ------------------------------------------------------------------
    EvalScenario(
        case_id="eval_standard_contract_001",
        question="员工数据给德国分公司，能不能走小规模个人信息出境的合同路径？",
        material_text=(
            "公司规模较小，处理约5万人个人信息，"
            "拟通过签订标准合同方式向德国分公司传输员工数据。已取得员工同意。"
        ),
        expected_sources=[
            "cac_personal_info_export_standard_contract_measures_2023",
            "cac_personal_info_export_standard_contract_filing_guide_v2_2024",
        ],
        expected_citation_roles=["primary_legal_basis"],
                should_abstain=False,
        tags=["standard_contract", "small_scale"],
    ),
    EvalScenario(
        case_id="eval_standard_contract_002",
        question="签完那份个人信息出境合同后，备案包里通常要放哪些材料？",
        material_text=(
            "我们准备用标准合同方式将客户信息传输至境外合作方，"
            "需要了解备案流程和所需材料。"
        ),
        expected_sources=[
            "cac_personal_info_export_standard_contract_filing_guide_v2_2024",
            "cac_personal_info_export_standard_contract_template_2023",
        ],
        expected_citation_roles=["primary_legal_basis"],
                should_abstain=False,
        tags=["standard_contract", "filing"],
    ),
    EvalScenario(
        case_id="eval_standard_contract_003",
        question="海外 HR 系统同步员工通讯录，应该准备哪类文件和备案？",
        material_text=(
            "集团要把境内员工姓名、手机号、邮箱同步到德国 HR 系统，"
            "全年涉及员工约3000人，已在员工告知书里说明用途。"
        ),
        expected_sources=[
            "cac_personal_info_export_standard_contract_measures_2023",
            "cac_personal_info_export_standard_contract_filing_guide_v2_2024",
        ],
        expected_citation_roles=["primary_legal_basis"],
                should_abstain=False,
        tags=["standard_contract", "implicit", "employee"],
    ),
    EvalScenario(
        case_id="eval_standard_contract_004",
        question="合同文本里境外接收方需要承诺哪些个人信息保护义务？",
        material_text=(
            "我们要审阅境外供应商提供的个人信息出境合同条款，"
            "重点想核对接收方安全措施、再转移、删除和个人权利响应等承诺。"
        ),
        expected_sources=[
            "cac_personal_info_export_standard_contract_template_2023",
            "cac_personal_info_export_standard_contract_measures_2023",
        ],
        expected_citation_roles=["primary_legal_basis"],
                should_abstain=False,
        tags=["standard_contract", "template", "obligations"],
    ),

    # ------------------------------------------------------------------
    # Smart connected vehicle / automotive
    # ------------------------------------------------------------------
    EvalScenario(
        case_id="eval_automotive_001",
        question="车端坐标和轨迹给海外算法团队，会碰到哪些汽车数据专项要求？",
        material_text=(
            "智能网联汽车采集车辆位置和行驶轨迹数据，"
            "需将数据传输至海外研发中心进行分析。"
        ),
        expected_sources=[
            "cac_automotive_data_security_provisions_2021",
            "cac_automotive_data_export_security_guide_2026",
            "cac_data_export_security_assessment_measures_2022",
        ],
        expected_citation_roles=[
            "conditional_industry_basis",
            "primary_legal_basis",
        ],
        should_abstain=False,
        tags=["automotive", "cross_border", "industry"],
    ),
    EvalScenario(
        case_id="eval_automotive_002",
        question="车企日常处理车主信息和车辆运行数据，行业安全要求看哪些依据？",
        material_text=(
            "汽车制造商在车辆生产和运营中处理车主个人信息和车辆运行数据，"
            "需要了解行业安全要求。"
        ),
        expected_sources=[
            "cac_automotive_data_security_provisions_2021",
            "mnr_intelligent_connected_vehicle_geoinformation_security_notice_2022",
        ],
        expected_citation_roles=["conditional_industry_basis"],
                should_abstain=False,
        tags=["automotive", "industry", "security"],
    ),
    EvalScenario(
        case_id="eval_automotive_003",
        question="自动驾驶采集道路影像和高精定位，地图相关安全边界看什么？",
        material_text=(
            "智能网联汽车测试车会采集道路影像、车辆位置和周边环境信息，"
            "算法团队可能用于高精地图和辅助驾驶模型训练。"
        ),
        expected_sources=[
            "mnr_intelligent_connected_vehicle_geoinformation_security_notice_2022",
            "cac_automotive_data_security_provisions_2021",
        ],
        expected_citation_roles=["conditional_industry_basis"],
                should_abstain=False,
        tags=["automotive", "mapping", "industry"],
    ),
    EvalScenario(
        case_id="eval_automotive_004",
        question="重庆自贸区里的车联网平台出境，除了全国规则还要看本地清单吗？",
        material_text=(
            "企业在重庆自贸试验区运营车联网平台，处理车辆运行、定位和道路环境数据，"
            "拟提供给境外研发团队。"
        ),
        expected_sources=[
            "chongqing_free_trade_zone_data_export_negative_list_2025",
            "cac_automotive_data_security_provisions_2021",
            "cac_cross_border_data_flow_rules_2024",
        ],
        expected_citation_roles=["conditional_local_basis", "conditional_industry_basis"],
        should_abstain=False,
        tags=["automotive", "regional", "chongqing", "cross_border"],
    ),

    # ------------------------------------------------------------------
    # Regional negative list
    # ------------------------------------------------------------------
    EvalScenario(
        case_id="eval_shanghai_001",
        question="临港业务的数据能不能自由传到境外，上海这边要先看哪张清单？",
        material_text=(
            "公司在上海自贸区开展业务，涉及用户数据出境，"
            "需要了解负面清单管理要求。"
        ),
        expected_sources=[
            "shanghai_free_trade_zone_data_export_negative_list_2024",
            "shanghai_free_trade_zone_data_export_negative_list_qna_2024",
            "cac_cross_border_data_flow_rules_2024",
        ],
        expected_citation_roles=["conditional_local_basis"],
        should_abstain=False,
        tags=["regional", "shanghai", "negative_list"],
    ),
    EvalScenario(
        case_id="eval_tianjin_001",
        question="天津自贸区企业做个人信息出境，地方清单会不会额外限制？",
        material_text=(
            "企业在天津自贸区注册，处理个人信息拟出境，"
            "想了解天津版负面清单的规定。"
        ),
        expected_sources=[
            "tianjin_free_trade_zone_data_export_negative_list_2024",
            "cac_cross_border_data_flow_rules_2024",
        ],
        expected_citation_roles=["conditional_local_basis"],
        should_abstain=False,
        tags=["regional", "tianjin", "negative_list"],
    ),
    EvalScenario(
        case_id="eval_hainan_001",
        question="海南旅游平台把游客数据给境外关联公司，要注意自贸港哪些本地规则？",
        material_text=(
            "公司在海南自贸港运营旅游平台，需要将游客数据传输至境外关联公司。"
        ),
        expected_sources=[
            "hainan_free_trade_port_data_export_negative_list_2024",
            "cac_cross_border_data_flow_rules_2024",
        ],
        expected_citation_roles=["conditional_local_basis"],
        should_abstain=False,
        tags=["regional", "hainan", "negative_list"],
    ),
    EvalScenario(
        case_id="eval_beijing_001",
        question="北京两区的文旅平台把游客画像给海外营销团队，地方清单怎么判断？",
        material_text=(
            "企业在北京自由贸易试验区内运营文旅平台，拟把游客画像、消费记录和联系方式"
            "提供给海外营销团队用于活动投放。"
        ),
        expected_sources=[
            "beijing_free_trade_zone_data_export_negative_list_2025",
            "cac_cross_border_data_flow_rules_2024",
        ],
        expected_citation_roles=["conditional_local_basis"],
        should_abstain=False,
        tags=["regional", "beijing", "negative_list"],
    ),
    EvalScenario(
        case_id="eval_zhejiang_001",
        question="浙江跨境电商订单和物流数据出境，地方口径看哪份材料？",
        material_text=(
            "平台在浙江自贸试验区经营跨境电商业务，计划把订单、物流、支付相关数据"
            "同步给境外仓和海外客服团队。"
        ),
        expected_sources=[
            "zhejiang_free_trade_zone_data_export_negative_list_2024",
            "cac_cross_border_data_flow_rules_2024",
        ],
        expected_citation_roles=["conditional_local_basis"],
        should_abstain=False,
        tags=["regional", "zhejiang", "ecommerce", "negative_list"],
    ),
    # ------------------------------------------------------------------
    # Sensitive personal information
    # ------------------------------------------------------------------
    EvalScenario(
        case_id="eval_sensitive_001",
        question="商场做人脸客流分析，个人信息保护上要重点看哪些依据？",
        material_text=(
            "公司计划在商场部署人脸识别系统进行客流分析，"
            "收集顾客人脸信息用于商业分析。"
        ),
        expected_sources=[
            "tc260_sensitive_pip_identification_guide_2024",
            "tc260_sensitive_pip_processing_requirements_2025",
            "flk_npc_ff8081817b6472a3017b656cc2040044",
        ],
        expected_citation_roles=["implementation_reference", "primary_legal_basis"],
                should_abstain=False,
        tags=["sensitive", "facial_recognition"],
    ),
    EvalScenario(
        case_id="eval_sensitive_002",
        question="App 想用精确位置给用户推附近优惠，这类信息算不算更敏感？",
        material_text=(
            "移动 App 会持续收集用户精确定位和到店记录，用于附近优惠推荐和广告投放，"
            "暂不涉及向境外提供。"
        ),
        expected_sources=[
            "flk_npc_ff8081817b6472a3017b656cc2040044",
            "tc260_sensitive_pip_identification_guide_2024",
            "tc260_sensitive_pip_processing_requirements_2025",
        ],
        expected_citation_roles=["primary_legal_basis", "implementation_reference"],
                should_abstain=False,
        tags=["sensitive", "location", "marketing"],
    ),
    # ------------------------------------------------------------------
    # Data classification / security governance
    # ------------------------------------------------------------------
    EvalScenario(
        case_id="eval_classification_001",
        question="业务数据先做分类分级，重要数据和一般数据的判定依据看哪里？",
        material_text=(
            "公司准备建立数据资产台账，覆盖交易记录、设备日志、用户资料和运营报表，"
            "希望先确定分类分级和重要数据识别口径。"
        ),
        expected_sources=[
            "tc260_gbt_43697_2024_data_classification_rules",
            "tc260_network_data_classification_guide_2021",
            "flk_npc_ff80818179f5e0800179f885c7e70392",
        ],
        expected_citation_roles=["implementation_reference", "primary_legal_basis"],
                should_abstain=False,
        tags=["classification", "data_security"],
    ),
    EvalScenario(
        case_id="eval_financial_001",
        question="金融信息服务里的用户行为日志和交易线索，该按什么行业口径分级？",
        material_text=(
            "金融信息服务平台处理用户浏览行为、投顾咨询记录、交易线索和账户相关标签，"
            "需要建立行业数据分类分级规则。"
        ),
        expected_sources=[
            "cac_financial_information_service_data_classification_guide_2026",
            "tc260_gbt_43697_2024_data_classification_rules",
        ],
        expected_citation_roles=["conditional_industry_basis", "implementation_reference"],
                should_abstain=False,
        tags=["classification", "financial", "industry"],
    ),

    # ------------------------------------------------------------------
    # Abstention case (insufficient info)
    # ------------------------------------------------------------------
    EvalScenario(
        case_id="eval_abstain_001",
        question="这个数据处理活动是否合规？",
        material_text="我们处理一些数据。",
        expected_sources=[],
        expected_citation_roles=[],
                should_abstain=True,
        must_not_cite_as_clause=[],
        tags=["abstention", "insufficient_info"],
    ),
    EvalScenario(
        case_id="eval_abstain_002",
        question="我们准备上线一个新功能，整体是否合规？",
        material_text="产品还在立项阶段，暂时没有确定会处理哪些数据、服务对象、地区或第三方合作方。",
        expected_sources=[],
        expected_citation_roles=[],
                should_abstain=True,
        must_not_cite_as_clause=[],
        tags=["abstention", "insufficient_info"],
    ),
]


def get_default_scenarios() -> list[EvalScenario]:
    """Return the full golden-set scenarios."""

    return get_scenarios("full")


def get_scenarios(suite: EvalSuite = "full") -> list[EvalScenario]:
    """Return scenarios for a named evaluation suite."""

    if suite == "base":
        return list(BASE_SCENARIOS)
    full = [*BASE_SCENARIOS, *FULL_EXTRA_SCENARIOS]
    if suite == "full":
        return full
    if suite == "quick":
        by_id = {scenario.case_id: scenario for scenario in full}
        missing = [case_id for case_id in QUICK_CASE_IDS if case_id not in by_id]
        if missing:
            raise RuntimeError(f"quick suite references unknown cases: {missing}")
        return [by_id[case_id] for case_id in QUICK_CASE_IDS]
    raise RuntimeError(f"unsupported eval suite: {suite!r}")


DEFAULT_SCENARIOS = get_default_scenarios()
