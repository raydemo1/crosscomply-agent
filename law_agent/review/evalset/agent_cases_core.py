"""Candidate legal-judgment cases; human approval is still outstanding."""

from law_agent.review.evalset.agent_schemas import AgentCase, AgentRubric, ScriptedAnswer
from law_agent.review.evalset.cases import get_scenarios

FLOW = "cac_cross_border_data_flow_rules_2024"
ASSESSMENT = "cac_data_export_security_assessment_measures_2022"
PIPL = "flk_npc_ff8081817b6472a3017b656cc2040044"
FLOW_URL = "https://www.cac.gov.cn/2024-03/22/c_1712776611775634.htm"
ASSESSMENT_URL = "https://www.cac.gov.cn/2022-07/07/c_1658811536396503.htm"


def build_core_cases() -> list[AgentCase]:
    scenarios = {s.case_id: s for s in get_scenarios("full")}

    def reuse(case_id: str, **values) -> AgentCase:
        scenario = scenarios[case_id]
        return AgentCase(
            case_id=case_id,
            source_case_id=case_id,
            question=scenario.question,
            material_text=scenario.material_text,
            tags=[*scenario.tags, "candidate"],
            **values,
        )

    confirmed_customer = {
        "contains_personal_information": True,
        "sensitive_personal_info": False,
        "cross_border_transfer": True,
        "important_data_status": "not_important",
        "ciio_status": "not_ciio",
        "annual_non_sensitive_count": "120000",
        "annual_sensitive_count": "0",
        "count_period": "current_year_cumulative",
        "overseas_recipient": "境外客服供应商",
        "processing_purpose": "客户售后客服",
        "exemption_facts": "已核实不属于个人合同履行必需、跨境人力资源管理、紧急救助或自贸区清单外等免予情形。",
        "notes": "审查时点2026-10-08；原材料的过去一年已核实为2026年1月1日至审查日的累计去重出境人数。只判断出境机制，不判断整体业务合规。",
    }
    first_round = [
        reuse(
            "eval_cross_border_008",
            selection_reason="区分现行出境人数门槛与旧规则，阻止把12万普通个人信息机械判为安全评估。",
            intake=confirmed_customer,
            rubric=AgentRubric(
                must_cover_sources=[FLOW],
                allowed_judgments=["已确认非CIIO、非重要数据、当年累计12万人普通个人信息且无豁免，适用标准合同或者认证路径；不因12万普通个人信息本身要求安全评估。"],
                forbidden_judgments=["仅按旧10万人门槛要求安全评估。", "认定无需标准合同或认证。", "把出境路径明确等同于业务整体合规。"],
                must_not_assume=["不得把处理总人数与累计出境人数混淆。"],
            ),
            reference_basis=[f"2024规定第七、八、十三条；{FLOW_URL}；2026-10-08核对官网及本地条款。"],
        ),
        reuse(
            "eval_standard_contract_003",
            selection_reason="避免看到员工数据就机械要求标准合同，检验第五条人力资源管理例外及剩余义务。",
            intake={
                **confirmed_customer,
                "annual_non_sensitive_count": "3000",
                "overseas_recipient": "德国集团HR系统",
                "processing_purpose": "跨境人力资源管理",
                "exemption_facts": "已核实依法制定劳动规章制度、依法签订集体合同；本次提供员工通讯录确为跨境人力资源管理所必需，不包括重要数据。",
                "notes": "审查时点2026-10-08；上述免予事实已确认。只判断出境机制，其他个人信息保护义务仍应依法核查。",
            },
            rubric=AgentRubric(
                must_cover_sources=[FLOW],
                allowed_judgments=["对已确认的跨境人力资源管理必要范围适用第五条第二项免予三种出境机制。"],
                forbidden_judgments=["无视已确认的第五条例外，一律要求标准合同备案。", "免予出境机制就代表无需任何个人信息保护义务。"],
                required_exceptions=["第五条第二项的劳动规章制度、集体合同、跨境HR管理必要性均已确认；免予机制不取消其他法定义务。"],
            ),
            reference_basis=[f"2024规定第五条第二项、第十条；{FLOW_URL}；2026-10-08核对官网及本地条款。"],
        ),
        reuse(
            "eval_cross_border_001",
            selection_reason="日均处理50万人不能替代当年出境累计人数；补充陈述不能自动升级已确认事实。",
            scripted_answers=[ScriptedAnswer(answer="申请人说明：估计当年出境12万人，没有核对台账；定位是否精确、CIIO身份和重要数据情况尚未核实。")],
            rubric=AgentRubric(
                should_abstain=True,
                acceptable_outcomes=["insufficient_evidence"],
                allowed_judgments=["指出缺口并给条件化路径，或追问后仍对未核实事实保留判断。"],
                forbidden_judgments=["依据日均50万用户直接给出确定的安全评估、合同、认证或免予结论。"],
                must_not_assume=["日均处理人数不是当年累计出境人数。", "申请人估计并未经过确认。", "不能默认CIIO、重要数据和敏感信息状态。"],
                clarification_expectations=["明确统计期间与去重出境人数、敏感信息类别与人数、CIIO及重要数据情况。"],
            ),
            reference_basis=[f"2024规定第五、七、八条；{FLOW_URL}。"],
        ),
        reuse(
            "eval_shanghai_001",
            selection_reason="检验地方清单的空间、行业与数据适用范围，注册在上海不等于免予。",
            rubric=AgentRubric(
                should_abstain=True,
                acceptable_outcomes=["insufficient_evidence"],
                allowed_judgments=["列明自贸区适用范围、数据类别、具体清单及全国规则关系尚需确认，给条件化判断。"],
                forbidden_judgments=["仅凭上海自贸区字样认定所有用户数据自由出境。", "未核对适用清单就确认清单外。"],
                must_not_assume=["材料未确认临港具体地点、适用行业、数据类型及清单内外。"],
                clarification_expectations=["具体业务所在地、清单适用范围、数据类别和出境数量。"],
                required_exceptions=["地方免予安排只在其适用范围内讨论；免予机制不等于整体合规。"],
            ),
            reference_basis=[f"2024规定第六条；{FLOW_URL}；地方适用结论待人工审定。"],
        ),
        reuse(
            "eval_sensitive_002",
            selection_reason="检验敏感信息判断及推荐性标准不能充当法律义务依据。",
            intake={"contains_personal_information": True, "sensitive_personal_info": True, "cross_border_transfer": False, "processing_purpose": "持续精确定位用于商业广告推荐", "notes": "材料中的精确定位和到店记录已确认可识别到个人；不判断未提供证据的具体违法事实。"},
            rubric=AgentRubric(
                must_cover_sources=[PIPL],
                forbidden_clause_sources=["tc260_sensitive_pip_identification_guide_2024", "tc260_sensitive_pip_processing_requirements_2025"],
                allowed_judgments=["区分敏感个人信息处理的必要性、单独同意、额外告知与保护要求，以及参考标准的辅助角色。"],
                forbidden_judgments=["未发生出境却要求出境安全评估。", "把TC260指南或推荐性标准作为具有强制法律效力的正式条款。", "无事实证据就宣布已经取得有效单独同意。"],
                must_not_assume=["材料未确认已取得单独同意、必要性已经满足、替代方案已经评估。"],
            ),
            reference_basis=["个人信息保护法第二十八至三十条；库内PIPL条款与TC260来源角色；具体业务是否违法待人工审定。"],
        ),
        AgentCase(
            case_id="agent_material_conflict_001",
            source_case_id="eval_cross_border_008",
            question="这次出境应该走标准合同还是安全评估？",
            material_text="材料A：经业务审批的2026年出境方案记载，非CIIO、无重要数据、无敏感个人信息，自2026年1月1日起累计出境12万人，客户数据用于境外商业分析，无特殊免予情形。\n材料B：同一方案的审计台账记载，自2026年1月1日起累计出境120万人。两份材料的统计期间、去重口径和业务范围相同，目前没有确认哪份数值正确。",
            tags=["candidate", "material_conflict", "fact_grounding"],
            selection_reason="只有Agent特有的材料冲突能力需要新增文本；不能自行择一或平均矛盾数字。",
            scripted_answers=[ScriptedAnswer(answer="两份材料都尚未更正，无法确认哪份数值正确；请保留冲突，不要自行选取数值。")],
            rubric=AgentRubric(
                should_abstain=True,
                acceptable_outcomes=["insufficient_evidence"],
                allowed_judgments=["指出12万与120万的同口径矛盾，说明不同数量将改变路径，暂停确定判断。"],
                forbidden_judgments=["自行择一、取均值或把两个互斥数值都视为已确认。"],
                must_not_assume=["不能把某一材料的数值当作已排除另一材料后的正确事实。"],
                clarification_expectations=["要求核对矛盾出境人数及更正证据，必要时重新冻结。"],
            ),
            reference_basis=[f"2024规定第七、八条；{FLOW_URL}；矛盾材料为评测合成输入。"],
        ),
        AgentCase(
            case_id="agent_historical_001",
            source_case_id="eval_cross_border_008",
            question="请仅按2023年12月31日当时已生效的规则，判断这次12万人普通个人信息出境是否触发安全评估。不要使用之后发布的豁免或新门槛。",
            material_text="历史档案：审查时点为2023年12月31日。境内普通电商不是CIIO，处理20万人个人信息，自2022年1月1日起累计向德国客服供应商提供12万名境内用户的姓名和手机号，无敏感个人信息，不涉及重要数据。只判断该时点的出境安全评估触发条件。",
            intake={"contains_personal_information": True, "sensitive_personal_info": False, "cross_border_transfer": True, "ciio_status": "not_ciio", "important_data_status": "not_important", "annual_non_sensitive_count": "120000", "annual_sensitive_count": "0", "count_period": "other", "notes": "已确认审查时点2023-12-31，人数统计期为自2022-01-01起累计；不属于2026年的现行审查。"},
            tags=["candidate", "historical", "temporal_applicability"],
            selection_reason="检验历史时点的规则选择，不能用2024新门槛覆盖2023年案件。",
            rubric=AgentRubric(
                must_cover_sources=[ASSESSMENT],
                allowed_judgments=["按2023-12-31有效的2022办法第四条第三项，12万人累计出境触发安全评估。"],
                forbidden_judgments=["把2024规定的100万人门槛追溯用于本案，断言只需合同或认证。"],
                must_not_assume=["统计期间已明确，不得改成当年累计或今日。"],
            ),
            reference_basis=[f"2022办法第四条第三项；{ASSESSMENT_URL}；2024规定第十四条生效时间为2024-03-22。"],
        ),
        reuse(
            "eval_out_of_corpus_001",
            selection_reason="受控库缺少欧盟AI Act，不得引用中国法强行完成外国法判断。",
            rubric=AgentRubric(
                should_abstain=True,
                acceptable_outcomes=["insufficient_evidence"],
                allowed_judgments=["明确适用法源不在受控库，披露能力边界并停止确定判断。"],
                forbidden_judgments=["以中国个人信息保护法等替代欧盟AI Act的高风险系统义务。", "凭模型记忆生成确定且无受控依据的欧盟法律结论。"],
            ),
            reference_basis=["库外法域边界案例；没有将EU AI Act实体法律规则编入rubric。"],
        ),
    ]
    from law_agent.review.evalset.agent_cases_second import build_second_cases

    return [*first_round, *build_second_cases(first_round)]
