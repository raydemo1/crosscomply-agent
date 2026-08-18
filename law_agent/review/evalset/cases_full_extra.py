"""Additional scenarios included only in the full review evaluation suite."""

from __future__ import annotations

from law_agent.review.evalset.schemas import EvalScenario

FULL_EXTRA_SCENARIOS: list[EvalScenario] = [
    # ------------------------------------------------------------------
    # Cross-border data export assessment
    # ------------------------------------------------------------------
    EvalScenario(
        case_id="eval_cross_border_006",
        question="向境外供应商提供重要数据时，安全评估是强制的吗？",
        material_text="制造企业拟将设备运行数据和生产质量数据提供给德国供应商协助诊断，其中部分数据已被内部标记为重要数据。",
        expected_sources=[
            "cac_data_export_security_assessment_measures_2022",
            "cac_cross_border_data_flow_rules_2024",
            "cac_data_export_security_assessment_filing_guide_v3_2025",
        ],
        expected_citation_roles=["primary_legal_basis"],
        tags=["cross_border", "assessment", "important_data"],
    ),
    EvalScenario(
        case_id="eval_cross_border_007",
        question="关键信息基础设施运营者向境外提供个人信息，申报口径看哪里？",
        material_text="某能源平台被认定为关键信息基础设施运营者，计划把运维账号、联系人信息和工单记录同步给境外技术支持团队。",
        expected_sources=[
            "cac_data_export_security_assessment_measures_2022",
            "cac_cross_border_data_flow_rules_2024",
            "flk_npc_ff8081817b63b935017b7bcc49877e0b",
        ],
        expected_citation_roles=["primary_legal_basis"],
        tags=["cross_border", "assessment", "ciio"],
    ),
    EvalScenario(
        case_id="eval_cross_border_008",
        question="累计向境外提供超过10万人个人信息，会触发安全评估吗？",
        material_text="过去一年公司通过海外客服系统处理约12万名境内用户的姓名、手机号和订单问题，后续还会继续同步。",
        expected_sources=[
            "cac_data_export_security_assessment_measures_2022",
            "cac_data_export_security_assessment_filing_guide_v3_2025",
            "cac_cross_border_data_flow_rules_2024",
        ],
        expected_citation_roles=["primary_legal_basis"],
        tags=["cross_border", "assessment", "volume_threshold"],
    ),
    EvalScenario(
        case_id="eval_cross_border_009",
        question="境外数据接收方要求再转移数据，原出境评估里要不要重新关注？",
        material_text="境外云服务商拟把部分日志再转移给其美国关联公司用于故障分析，原合同只写明由新加坡主体处理。",
        expected_sources=[
            "cac_data_export_security_assessment_measures_2022",
            "cac_personal_info_export_standard_contract_template_2023",
            "cac_data_export_security_assessment_filing_guide_v3_2025",
        ],
        expected_citation_roles=["primary_legal_basis"],
        tags=["cross_border", "onward_transfer", "change"],
    ),
    EvalScenario(
        case_id="eval_cross_border_010",
        question="安全评估有效期快到了，续期和重新申报有什么依据？",
        material_text="公司两年前通过数据出境安全评估，接收方、目的和数据范围基本未变，现在想确认有效期届满前如何处理。",
        expected_sources=[
            "cac_data_export_security_assessment_filing_guide_v3_2025",
            "cac_data_export_security_assessment_measures_2022",
        ],
        expected_citation_roles=["primary_legal_basis"],
        tags=["cross_border", "assessment", "validity"],
    ),
    EvalScenario(
        case_id="eval_cross_border_011",
        question="低频向境外律师提供少量员工资料，是否一定要走安全评估？",
        material_text="公司因境外劳动争议，需要向德国律师提供3名员工的姓名、岗位和合同信息，不涉及大规模用户数据。",
        expected_sources=[
            "cac_cross_border_data_flow_rules_2024",
            "cac_personal_info_export_standard_contract_measures_2023",
        ],
        expected_citation_roles=["primary_legal_basis"],
        tags=["cross_border", "small_scale", "standard_contract"],
    ),
    EvalScenario(
        case_id="eval_cross_border_012",
        question="只把匿名化统计报表发给境外总部，还算数据出境安全评估场景吗？",
        material_text="境内团队每月向新加坡总部发送按城市汇总后的销售统计，不含明细订单、姓名、手机号或设备标识。",
        expected_sources=[
            "cac_cross_border_data_flow_rules_2024",
            "cac_data_export_policy_qna_2025_04",
        ],
        expected_citation_roles=["primary_legal_basis", "interpretation_auxiliary"],
        tags=["cross_border", "anonymized", "boundary"],
    ),
    EvalScenario(
        case_id="eval_cross_border_013",
        question="境外远程访问境内数据库，是不是也按数据出境看？",
        material_text="海外运维人员通过 VPN 远程查看境内用户数据库中的订单和联系方式，数据没有批量下载到境外服务器。",
        expected_sources=[
            "cac_data_export_security_assessment_measures_2022",
            "cac_cross_border_data_flow_rules_2024",
            "cac_data_export_assessment_qna_2022",
        ],
        expected_citation_roles=["primary_legal_basis", "interpretation_auxiliary"],
        tags=["cross_border", "remote_access"],
    ),
    EvalScenario(
        case_id="eval_cross_border_014",
        question="数据出境负面清单之外的活动，还需要看全国安全评估规则吗？",
        material_text="企业位于自贸区，认为本地负面清单未列入其业务数据，因此想确认是否完全不需要看全国出境规则。",
        expected_sources=[
            "cac_cross_border_data_flow_rules_2024",
            "cac_data_export_security_assessment_measures_2022",
        ],
        expected_citation_roles=["primary_legal_basis"],
        tags=["cross_border", "regional", "boundary"],
    ),
    # ------------------------------------------------------------------
    # Standard contract and filing
    # ------------------------------------------------------------------
    EvalScenario(
        case_id="eval_standard_contract_005",
        question="标准合同签订后多久内要备案？",
        material_text="公司计划通过个人信息出境标准合同方式向境外客服供应商提供客户联系方式，想确认签署后的备案时限。",
        expected_sources=[
            "cac_personal_info_export_standard_contract_measures_2023",
            "cac_personal_info_export_standard_contract_filing_guide_v2_2024",
        ],
        expected_citation_roles=["primary_legal_basis"],
        tags=["standard_contract", "filing", "deadline"],
    ),
    EvalScenario(
        case_id="eval_standard_contract_006",
        question="标准合同备案前，个人信息保护影响评估报告是不是必须准备？",
        material_text="境内 App 运营者准备把用户邮箱和订单号传给境外 SaaS 工具，准备用标准合同路径办理。",
        expected_sources=[
            "cac_personal_info_export_standard_contract_measures_2023",
            "cac_personal_info_export_standard_contract_filing_guide_v2_2024",
        ],
        expected_citation_roles=["primary_legal_basis"],
        tags=["standard_contract", "impact_assessment"],
    ),
    EvalScenario(
        case_id="eval_standard_contract_007",
        question="境外接收方变更时，标准合同备案要重新处理吗？",
        material_text="公司原标准合同对应的境外接收方是德国分公司，现在改为美国供应商，数据类型和目的也有所调整。",
        expected_sources=[
            "cac_personal_info_export_standard_contract_measures_2023",
            "cac_personal_info_export_standard_contract_filing_guide_v2_2024",
        ],
        expected_citation_roles=["primary_legal_basis"],
        tags=["standard_contract", "change"],
    ),
    EvalScenario(
        case_id="eval_standard_contract_008",
        question="标准合同和认证都能用时，怎么区分合同路径的适用条件？",
        material_text="企业不是关键信息基础设施运营者，处理个人信息规模较小，拟向境外集团公司提供员工和客户联系人信息。",
        expected_sources=[
            "cac_personal_info_export_standard_contract_measures_2023",
            "cac_personal_info_export_certification_measures_2025",
            "cac_cross_border_flow_rules_qna_2024",
        ],
        expected_citation_roles=["primary_legal_basis", "interpretation_auxiliary"],
        tags=["standard_contract", "certification", "path_selection"],
    ),
    EvalScenario(
        case_id="eval_standard_contract_009",
        question="境外接收方不配合标准合同条款，境内处理者还能继续传吗？",
        material_text="境外供应商拒绝承诺协助响应个人权利请求，也不接受审计条款，但业务部门仍希望继续同步客户资料。",
        expected_sources=[
            "cac_personal_info_export_standard_contract_template_2023",
            "cac_personal_info_export_standard_contract_measures_2023",
        ],
        expected_citation_roles=["primary_legal_basis"],
        tags=["standard_contract", "recipient_obligations"],
    ),
    EvalScenario(
        case_id="eval_standard_contract_010",
        question="用标准合同出境时，合同范本能不能自行删减核心条款？",
        material_text="境外合作方希望删除个人信息主体权利响应、再转移限制和安全事件通知条款，只保留商业服务内容。",
        expected_sources=[
            "cac_personal_info_export_standard_contract_template_2023",
            "cac_personal_info_export_standard_contract_measures_2023",
        ],
        expected_citation_roles=["primary_legal_basis"],
        tags=["standard_contract", "template"],
    ),
    # ------------------------------------------------------------------
    # Personal information protection certification
    # ------------------------------------------------------------------
    EvalScenario(
        case_id="eval_certification_001",
        question="集团内部跨境共享个人信息，个人信息保护认证路径看哪些依据？",
        material_text="跨国集团希望由境内子公司向境外母公司持续提供员工和客户联系人信息，计划考虑个人信息保护认证。",
        expected_sources=[
            "cac_personal_info_export_certification_measures_2025",
            "cac_personal_info_protection_certification_rules_2022",
            "tc260_cross_border_certification_spec_2022",
        ],
        expected_citation_roles=["primary_legal_basis", "implementation_reference"],
        tags=["certification", "cross_border", "group"],
    ),
    EvalScenario(
        case_id="eval_certification_002",
        question="个人信息出境认证申请，需要证明哪些保护能力？",
        material_text="企业准备申请个人信息出境认证，需要梳理组织管理、个人权利保障、接收方约束和安全措施。",
        expected_sources=[
            "cac_personal_info_export_certification_measures_2025",
            "tc260_cross_border_certification_spec_2022",
            "cac_personal_info_protection_certification_rules_2022",
        ],
        expected_citation_roles=["primary_legal_basis", "implementation_reference"],
        tags=["certification", "security_controls"],
    ),
    EvalScenario(
        case_id="eval_certification_003",
        question="大湾区内地和香港之间传个人信息，有没有专门保护要求？",
        material_text="企业位于广东，拟向香港关联公司提供客户姓名、联系方式和服务记录，用于跨境客户服务协同。",
        expected_sources=[
            "tc260_gba_cross_border_pip_protection_2024",
            "tc260_cross_border_certification_spec_2022",
            "cac_personal_info_export_certification_measures_2025",
        ],
        expected_citation_roles=["implementation_reference", "primary_legal_basis"],
        tags=["certification", "gba", "hong_kong"],
    ),
    EvalScenario(
        case_id="eval_certification_004",
        question="认证路径下发生个人信息安全事件，境内外主体责任怎么找依据？",
        material_text="境外接收方系统发生泄露，涉及境内用户联系方式和服务记录，集团希望确认认证路径下的责任和处置要求。",
        expected_sources=[
            "cac_personal_info_export_certification_measures_2025",
            "tc260_cross_border_certification_spec_2022",
            "flk_npc_ff8081817b6472a3017b656cc2040044",
        ],
        expected_citation_roles=["primary_legal_basis", "implementation_reference"],
        tags=["certification", "incident"],
    ),
    EvalScenario(
        case_id="eval_certification_005",
        question="个人信息保护认证和标准合同备案是不是可以互相替代？",
        material_text="企业同时听说可以走认证和标准合同，希望判断两种个人信息出境路径的关系和适用边界。",
        expected_sources=[
            "cac_cross_border_flow_rules_qna_2024",
            "cac_personal_info_export_certification_measures_2025",
            "cac_personal_info_export_standard_contract_measures_2023",
        ],
        expected_citation_roles=["interpretation_auxiliary", "primary_legal_basis"],
        tags=["certification", "standard_contract", "path_selection"],
    ),
    EvalScenario(
        case_id="eval_certification_006",
        question="境外接收方是受托处理者，认证材料要不要覆盖委托处理关系？",
        material_text="境内平台把用户咨询记录提供给境外客服外包商处理，考虑通过个人信息保护认证证明跨境保护能力。",
        expected_sources=[
            "cac_personal_info_export_certification_measures_2025",
            "tc260_cross_border_certification_spec_2022",
        ],
        expected_citation_roles=["primary_legal_basis", "implementation_reference"],
        tags=["certification", "processor"],
    ),
    # ------------------------------------------------------------------
    # Automotive and smart connected vehicle data
    # ------------------------------------------------------------------
    EvalScenario(
        case_id="eval_automotive_005",
        question="车企处理车外影像数据，要按汽车重要数据管理吗？",
        material_text="智能汽车在公共道路测试时采集车外视频、道路标识和行人轨迹，企业想确认汽车数据安全管理依据。",
        expected_sources=[
            "cac_automotive_data_security_provisions_2021",
            "mnr_intelligent_connected_vehicle_geoinformation_security_notice_2022",
        ],
        expected_citation_roles=["conditional_industry_basis"],
        tags=["automotive", "important_data", "mapping"],
    ),
    EvalScenario(
        case_id="eval_automotive_006",
        question="汽车数据出境安全指引和正式出境规则之间怎么配合看？",
        material_text="车企拟把车辆运行状态、故障日志和定位轨迹提供给境外研发中心，用于算法迭代。",
        expected_sources=[
            "cac_automotive_data_export_security_guide_2026",
            "cac_automotive_data_security_provisions_2021",
            "cac_data_export_security_assessment_measures_2022",
        ],
        expected_citation_roles=["conditional_industry_basis", "primary_legal_basis"],
        tags=["automotive", "cross_border", "guideline"],
    ),
    EvalScenario(
        case_id="eval_automotive_007",
        question="车联网平台收集车主手机号和行驶位置，个人信息保护依据看什么？",
        material_text="车联网 App 收集车主手机号、车辆绑定关系、实时位置和行驶路线，用于远程控车和服务提醒。",
        expected_sources=[
            "cac_automotive_data_security_provisions_2021",
            "flk_npc_ff8081817b6472a3017b656cc2040044",
            "tc260_sensitive_pip_identification_guide_2024",
        ],
        expected_citation_roles=["conditional_industry_basis", "primary_legal_basis"],
        tags=["automotive", "personal_info", "location"],
    ),
    EvalScenario(
        case_id="eval_automotive_008",
        question="把道路采集数据用于高精地图训练，测绘地理信息安全怎么判断？",
        material_text="自动驾驶测试车辆采集道路影像、交通标志、经纬度轨迹和道路设施信息，用于地图更新和模型训练。",
        expected_sources=[
            "mnr_intelligent_connected_vehicle_geoinformation_security_notice_2022",
            "cac_automotive_data_security_provisions_2021",
        ],
        expected_citation_roles=["conditional_industry_basis"],
        tags=["automotive", "mapping", "geographic_information"],
    ),
    EvalScenario(
        case_id="eval_automotive_009",
        question="汽车数据处理者年度报告义务从哪些文件找？",
        material_text="车企在境内运营联网车辆服务，处理车主个人信息、车辆运行数据和车外环境数据，希望确认持续合规义务。",
        expected_sources=[
            "cac_automotive_data_security_provisions_2021",
            "cac_automotive_data_export_security_guide_2026",
        ],
        expected_citation_roles=["conditional_industry_basis"],
        tags=["automotive", "ongoing_obligations"],
    ),
    EvalScenario(
        case_id="eval_automotive_010",
        question="汽车数据跨境时，如果涉及重要数据，申报材料要看哪些？",
        material_text="车企将被识别为重要数据的道路环境数据提供给境外算法团队，准备办理数据出境安全评估。",
        expected_sources=[
            "cac_automotive_data_security_provisions_2021",
            "cac_automotive_data_export_security_guide_2026",
            "cac_data_export_security_assessment_filing_guide_v3_2025",
        ],
        expected_citation_roles=["conditional_industry_basis", "primary_legal_basis"],
        tags=["automotive", "important_data", "assessment"],
    ),
    # ------------------------------------------------------------------
    # Financial data and classification
    # ------------------------------------------------------------------
    EvalScenario(
        case_id="eval_financial_002",
        question="金融信息服务平台做数据分类分级，行业指南和国标怎么一起用？",
        material_text="平台处理行情浏览、投顾咨询、交易意向和用户画像标签，准备建立金融信息服务数据分级目录。",
        expected_sources=[
            "cac_financial_information_service_data_classification_guide_2026",
            "tc260_gbt_43697_2024_data_classification_rules",
            "tc260_network_data_classification_guide_2021",
        ],
        expected_citation_roles=["conditional_industry_basis", "implementation_reference"],
        tags=["financial", "classification"],
    ),
    EvalScenario(
        case_id="eval_financial_003",
        question="金融用户画像和交易线索能不能按一般数据处理？",
        material_text="金融资讯平台希望把用户风险偏好、浏览行为、交易线索和账户标签用于精准营销。",
        expected_sources=[
            "cac_financial_information_service_data_classification_guide_2026",
            "flk_npc_ff8081817b6472a3017b656cc2040044",
            "tc260_sensitive_pip_processing_requirements_2025",
        ],
        expected_citation_roles=["conditional_industry_basis", "primary_legal_basis"],
        tags=["financial", "personal_info", "classification"],
    ),
    EvalScenario(
        case_id="eval_financial_004",
        question="金融信息服务数据出境时，行业分类分级是否会影响评估？",
        material_text="金融信息服务平台拟向境外分析团队提供用户行为日志、资讯订阅偏好和投顾咨询摘要。",
        expected_sources=[
            "cac_financial_information_service_data_classification_guide_2026",
            "cac_data_export_security_assessment_measures_2022",
            "cac_cross_border_data_flow_rules_2024",
        ],
        expected_citation_roles=["conditional_industry_basis", "primary_legal_basis"],
        tags=["financial", "cross_border", "classification"],
    ),
    EvalScenario(
        case_id="eval_financial_005",
        question="金融信息服务企业只做内部分类分级，是否需要引用数据安全法？",
        material_text="公司准备对金融资讯数据、用户行为日志、客户服务记录和运营报表建立分类分级制度。",
        expected_sources=[
            "flk_npc_ff80818179f5e0800179f885c7e70392",
            "cac_financial_information_service_data_classification_guide_2026",
            "tc260_gbt_43697_2024_data_classification_rules",
        ],
        expected_citation_roles=["primary_legal_basis", "conditional_industry_basis"],
        tags=["financial", "classification", "data_security_law"],
    ),
    # ------------------------------------------------------------------
    # Regional negative lists and local rules
    # ------------------------------------------------------------------
    EvalScenario(
        case_id="eval_fujian_001",
        question="福建自贸区数据出境负面清单要看哪份？",
        material_text="企业在福建自贸试验区开展跨境电商业务，拟把订单、物流和售后信息同步给境外仓。",
        expected_sources=[
            "fujian_free_trade_zone_data_export_negative_list_2025",
            "cac_cross_border_data_flow_rules_2024",
        ],
        expected_citation_roles=["conditional_local_basis", "primary_legal_basis"],
        tags=["regional", "fujian", "negative_list"],
    ),
    EvalScenario(
        case_id="eval_guangdong_001",
        question="广东自贸区企业把客户资料给境外合作方，地方负面清单看哪里？",
        material_text="企业位于广东自贸试验区，拟向境外合作伙伴提供客户联系方式、订单和服务记录。",
        expected_sources=[
            "guangdong_free_trade_zone_data_export_negative_list_2025",
            "cac_cross_border_data_flow_rules_2024",
        ],
        expected_citation_roles=["conditional_local_basis", "primary_legal_basis"],
        tags=["regional", "guangdong", "negative_list"],
    ),
    EvalScenario(
        case_id="eval_jiangsu_001",
        question="江苏自贸区制造业数据出境，地方负面清单是否覆盖？",
        material_text="江苏自贸试验区内制造企业计划把设备运行数据和供应链数据发给境外集团，用于生产协同。",
        expected_sources=[
            "jiangsu_free_trade_zone_data_export_negative_list_2025",
            "cac_cross_border_data_flow_rules_2024",
        ],
        expected_citation_roles=["conditional_local_basis", "primary_legal_basis"],
        tags=["regional", "jiangsu", "negative_list"],
    ),
    EvalScenario(
        case_id="eval_guangxi_001",
        question="广西自贸区面向东盟业务的数据出境，地方清单怎么查？",
        material_text="企业在广西自贸试验区运营跨境物流平台，拟向东盟合作方共享订单、车辆和客户联系信息。",
        expected_sources=[
            "guangxi_free_trade_zone_data_export_negative_list_2025",
            "cac_cross_border_data_flow_rules_2024",
        ],
        expected_citation_roles=["conditional_local_basis", "primary_legal_basis"],
        tags=["regional", "guangxi", "negative_list"],
    ),
    EvalScenario(
        case_id="eval_shenzhen_001",
        question="深圳企业处理个人数据和公共数据，地方数据条例要不要引用？",
        material_text="深圳企业建设城市服务平台，处理用户注册资料、办事记录和部分公共数据资源，想确认地方数据条例依据。",
        expected_sources=[
            "shenzhen_data_regulation_2021",
            "flk_npc_ff80818179f5e0800179f885c7e70392",
        ],
        expected_citation_roles=["conditional_local_basis", "primary_legal_basis"],
        tags=["regional", "shenzhen", "data_regulation"],
    ),
    EvalScenario(
        case_id="eval_regional_compare_001",
        question="同一个跨境电商业务在浙江和福建自贸区，地方清单能混用吗？",
        material_text="集团分别在浙江和福建自贸试验区运营跨境电商业务，想比较订单和物流数据出境时的地方负面清单。",
        expected_sources=[
            "zhejiang_free_trade_zone_data_export_negative_list_2024",
            "fujian_free_trade_zone_data_export_negative_list_2025",
            "cac_cross_border_data_flow_rules_2024",
        ],
        expected_citation_roles=["conditional_local_basis", "primary_legal_basis"],
        tags=["regional", "zhejiang", "fujian", "compare"],
    ),
    EvalScenario(
        case_id="eval_regional_compare_002",
        question="广东和海南的旅游消费数据出境清单是否都要看？",
        material_text="集团在广东和海南都有旅游消费业务，计划统一向境外营销平台提供游客画像和消费记录。",
        expected_sources=[
            "guangdong_free_trade_zone_data_export_negative_list_2025",
            "hainan_free_trade_port_data_export_negative_list_2024",
            "cac_cross_border_data_flow_rules_2024",
        ],
        expected_citation_roles=["conditional_local_basis", "primary_legal_basis"],
        tags=["regional", "guangdong", "hainan", "compare"],
    ),
    EvalScenario(
        case_id="eval_regional_boundary_001",
        question="企业注册在上海但业务不在自贸区，能直接适用上海自贸区负面清单吗？",
        material_text="公司注册地在上海市区，业务系统和数据处理活动不在临港新片区或上海自贸试验区内。",
        expected_sources=[
            "shanghai_free_trade_zone_data_export_negative_list_2024",
            "cac_cross_border_data_flow_rules_2024",
        ],
        expected_citation_roles=["conditional_local_basis", "primary_legal_basis"],
        tags=["regional", "shanghai", "boundary"],
    ),
    EvalScenario(
        case_id="eval_regional_boundary_002",
        question="只有全国业务没有具体地方落点，是否应该强行套地方负面清单？",
        material_text="平台面向全国用户提供服务，数据处理地点和自贸区主体暂未确定，只知道可能存在境外客服访问。",
        expected_sources=[
            "cac_cross_border_data_flow_rules_2024",
            "cac_data_export_security_assessment_measures_2022",
        ],
        expected_citation_roles=["primary_legal_basis"],
        tags=["regional", "boundary", "no_local_region"],
    ),
    # ------------------------------------------------------------------
    # TC260 / GB/T standards and implementation references
    # ------------------------------------------------------------------
    EvalScenario(
        case_id="eval_tc260_pip_001",
        question="个人信息安全规范能用来补充哪些管理要求？",
        material_text="App 运营者希望建立个人信息收集、使用、删除、委托处理和安全事件管理制度，需要参考国家标准。",
        expected_sources=[
            "tc260_gbt_35273_2020_pip_security_spec",
            "flk_npc_ff8081817b6472a3017b656cc2040044",
        ],
        expected_citation_roles=["implementation_reference", "primary_legal_basis"],
        tags=["tc260", "gbt", "personal_info"],
    ),
    EvalScenario(
        case_id="eval_tc260_sensitive_001",
        question="敏感个人信息识别和处理安全要求分别看哪些标准？",
        material_text="平台处理精确定位、人脸图片、儿童账号和健康标签，需要识别敏感个人信息并制定额外保护措施。",
        expected_sources=[
            "tc260_sensitive_pip_identification_guide_2024",
            "tc260_sensitive_pip_processing_requirements_2025",
            "flk_npc_ff8081817b6472a3017b656cc2040044",
        ],
        expected_citation_roles=["implementation_reference", "primary_legal_basis"],
        tags=["tc260", "sensitive", "personal_info"],
    ),
    EvalScenario(
        case_id="eval_tc260_classification_001",
        question="数据分类分级规则和网络数据分类分级指引有什么参考价值？",
        material_text="企业要梳理客户资料、交易数据、日志数据、研发数据和公开信息，建立统一的数据分类分级制度。",
        expected_sources=[
            "tc260_gbt_43697_2024_data_classification_rules",
            "tc260_network_data_classification_guide_2021",
            "flk_npc_ff80818179f5e0800179f885c7e70392",
        ],
        expected_citation_roles=["implementation_reference", "primary_legal_basis"],
        tags=["tc260", "classification"],
    ),
    EvalScenario(
        case_id="eval_tc260_cross_border_001",
        question="个人信息跨境处理活动安全认证规范能作为哪些操作参考？",
        material_text="企业准备通过认证路径向境外关联公司提供个人信息，需要细化保护协议、个人权利响应和安全措施。",
        expected_sources=[
            "tc260_cross_border_certification_spec_2022",
            "cac_personal_info_export_certification_measures_2025",
        ],
        expected_citation_roles=["implementation_reference", "primary_legal_basis"],
        tags=["tc260", "certification", "cross_border"],
    ),
    EvalScenario(
        case_id="eval_tc260_gba_001",
        question="粤港澳大湾区个人信息跨境保护要求适合什么场景？",
        material_text="广东企业拟向香港服务团队提供会员信息和售后记录，用于跨境客户服务，希望参考大湾区个人信息保护要求。",
        expected_sources=[
            "tc260_gba_cross_border_pip_protection_2024",
            "cac_personal_info_export_certification_measures_2025",
        ],
        expected_citation_roles=["implementation_reference", "primary_legal_basis"],
        tags=["tc260", "gba", "cross_border"],
    ),
    EvalScenario(
        case_id="eval_tc260_security_controls_001",
        question="做个人信息保护整改时，法律依据和标准依据怎么搭配？",
        material_text="公司被要求整改个人信息告知同意、最小必要、访问控制和删除响应机制，想同时找到法律和标准依据。",
        expected_sources=[
            "flk_npc_ff8081817b6472a3017b656cc2040044",
            "tc260_gbt_35273_2020_pip_security_spec",
            "tc260_sensitive_pip_processing_requirements_2025",
        ],
        expected_citation_roles=["primary_legal_basis", "implementation_reference"],
        tags=["tc260", "personal_info", "security_controls"],
    ),
    # ------------------------------------------------------------------
    # Official Q&A and interpretation materials
    # ------------------------------------------------------------------
    EvalScenario(
        case_id="eval_qna_001",
        question="官方问答里怎么解释数据出境安全评估的申报情形？",
        material_text="业务团队希望用官方解释材料向管理层说明哪些场景需要申报数据出境安全评估。",
        expected_sources=[
            "cac_data_export_assessment_qna_2022",
            "cac_data_export_security_assessment_measures_2022",
        ],
        expected_citation_roles=["interpretation_auxiliary", "primary_legal_basis"],
        tags=["qna", "cross_border", "assessment"],
    ),
    EvalScenario(
        case_id="eval_qna_002",
        question="2024 年促进数据跨境流动规定的官方问答适合解释哪些路径关系？",
        material_text="合规负责人要向业务解释安全评估、标准合同、个人信息保护认证之间的适用关系。",
        expected_sources=[
            "cac_cross_border_flow_rules_qna_2024",
            "cac_cross_border_data_flow_rules_2024",
        ],
        expected_citation_roles=["interpretation_auxiliary", "primary_legal_basis"],
        tags=["qna", "cross_border", "path_selection"],
    ),
    EvalScenario(
        case_id="eval_qna_003",
        question="2025 年数据出境政策问答能否解释便利化政策？",
        material_text="企业想了解数据出境便利化政策、负面清单和免申报场景的官方解释口径。",
        expected_sources=[
            "cac_data_export_policy_qna_2025_04",
            "cac_cross_border_data_flow_rules_2024",
        ],
        expected_citation_roles=["interpretation_auxiliary", "primary_legal_basis"],
        tags=["qna", "policy", "cross_border"],
    ),
    EvalScenario(
        case_id="eval_qna_004",
        question="网络数据安全管理条例答记者问能帮助解释哪些制度重点？",
        material_text="企业希望理解网络数据安全管理条例关于重要数据、个人信息、平台义务和监督管理的制度背景。",
        expected_sources=[
            "cac_network_data_security_regulation_qna_2024",
            "flk_npc_ff808181927f0e7b0192949a1da4355d",
        ],
        expected_citation_roles=["interpretation_auxiliary", "primary_legal_basis"],
        tags=["qna", "network_data_security"],
    ),
    # ------------------------------------------------------------------
    # Insufficient evidence, out-of-corpus, conflict, and boundary scenarios
    # ------------------------------------------------------------------
    EvalScenario(
        case_id="eval_abstain_003",
        question="我们要做数据合规，应该怎么做？",
        material_text="目前只有一个产品想法，还没有确定数据类型、用户范围、处理目的、地区或第三方接收方。",
        expected_sources=[],
        should_abstain=True,
        min_recall_at_5=0.0,
        tags=["abstention", "insufficient_info"],
    ),
    EvalScenario(
        case_id="eval_abstain_004",
        question="这个 AI 模型训练方案是否合法？",
        material_text="业务只说会用一些资料训练模型，没有说明资料来源、是否含个人信息、是否跨境、是否公开数据。",
        expected_sources=[],
        should_abstain=True,
        min_recall_at_5=0.0,
        tags=["abstention", "insufficient_info", "ai"],
    ),
    EvalScenario(
        case_id="eval_out_of_corpus_001",
        question="欧盟 AI Act 对高风险 AI 系统有什么要求？",
        material_text="公司准备在欧盟上线招聘筛选 AI，希望了解 EU AI Act 的高风险系统义务。",
        expected_sources=[],
        should_abstain=True,
        min_recall_at_5=0.0,
        tags=["abstention", "out_of_corpus", "eu_ai_act"],
    ),
    EvalScenario(
        case_id="eval_out_of_corpus_002",
        question="美国加州 CCPA 对数据销售的 opt-out 怎么做？",
        material_text="企业计划向美国广告平台共享加州居民数据，希望了解 CCPA/CPRA 的退出机制。",
        expected_sources=[],
        should_abstain=True,
        min_recall_at_5=0.0,
        tags=["abstention", "out_of_corpus", "ccpa"],
    ),
    EvalScenario(
        case_id="eval_boundary_001",
        question="只做境内委托处理，不出境，是否需要标准合同备案？",
        material_text="境内 App 把用户手机号和订单号提供给境内客服外包商，数据和访问权限都限制在中国境内。",
        expected_sources=[
            "flk_npc_ff8081817b6472a3017b656cc2040044",
            "cac_personal_info_export_standard_contract_measures_2023",
        ],
        expected_citation_roles=["primary_legal_basis"],
        tags=["boundary", "no_cross_border", "standard_contract"],
    ),
    EvalScenario(
        case_id="eval_boundary_002",
        question="处理儿童个人信息和精确定位，是否一定触发数据出境安全评估？",
        material_text="App 在境内处理儿童账号信息和精确定位，用于家长守护功能，暂不向境外提供。",
        expected_sources=[
            "flk_npc_ff8081817b6472a3017b656cc2040044",
            "tc260_sensitive_pip_identification_guide_2024",
            "tc260_sensitive_pip_processing_requirements_2025",
        ],
        expected_citation_roles=["primary_legal_basis", "implementation_reference"],
        tags=["boundary", "sensitive", "no_cross_border"],
    ),
    EvalScenario(
        case_id="eval_conflict_001",
        question="地方负面清单说不在清单内，全国规则又有安全评估门槛，应该怎么判断？",
        material_text="企业在广东自贸区，业务数据未出现在地方负面清单描述中，但预计一年向境外提供超过100万人个人信息。",
        expected_sources=[
            "guangdong_free_trade_zone_data_export_negative_list_2025",
            "cac_cross_border_data_flow_rules_2024",
            "cac_data_export_security_assessment_measures_2022",
        ],
        expected_citation_roles=["conditional_local_basis", "primary_legal_basis"],
        tags=["conflict", "regional", "assessment_threshold"],
    ),
    EvalScenario(
        case_id="eval_conflict_002",
        question="标准合同路径和安全评估门槛同时出现，哪个优先？",
        material_text="公司希望走标准合同，但平台已处理超过100万人个人信息，并计划向境外分析平台持续提供用户行为数据。",
        expected_sources=[
            "cac_data_export_security_assessment_measures_2022",
            "cac_personal_info_export_standard_contract_measures_2023",
            "cac_cross_border_flow_rules_qna_2024",
        ],
        expected_citation_roles=["primary_legal_basis", "interpretation_auxiliary"],
        tags=["conflict", "standard_contract", "assessment"],
    ),
]
