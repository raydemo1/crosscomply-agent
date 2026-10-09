"""Unrun synthetic candidates reserved for review before a new evaluation."""

from law_agent.review.evalset.agent_cases_core import FLOW, FLOW_URL
from law_agent.review.evalset.agent_schemas import AgentCase, AgentRubric


def build_holdout_cases() -> list[AgentCase]:
    common = {
        "contains_personal_information": True,
        "cross_border_transfer": True,
        "sensitive_personal_info": False,
        "ciio_status": "not_ciio",
        "important_data_status": "not_important",
        "count_period": "current_year_cumulative",
        "annual_sensitive_count": "0",
        "notes": "审查时点2024-03-23，仅判断本次出境机制。结构化填报与材料一致；未明确的手续完成情况不作确认。",
    }

    def case(
        case_id: str, material: str, intake: dict, reason: str,
        allowed: list[str], forbidden: list[str], articles: str,
        *, clarification: list[str] | None = None,
    ) -> AgentCase:
        return AgentCase(
            case_id=case_id,
            question="请按2024年3月23日已生效的规则判断本次数据出境机制，并说明适用前提与判断边界。",
            material_text=material,
            intake={**common, **intake},
            tags=["candidate", "holdout_candidate", "synthetic"],
            selection_reason=reason,
            rubric=AgentRubric(
                should_abstain=None,
                must_cover_sources=[FLOW],
                allowed_judgments=allowed,
                forbidden_judgments=forbidden,
                clarification_expectations=clarification or [],
                must_not_assume=["机制选择不代表整体合规或已经完成法定手续。"],
            ),
            reference_basis=[f"2024规定{articles}；{FLOW_URL}；官网核对仅形成候选依据，尚未人工法律审定。"],
        )

    return [
        case(
            "holdout_foreign_origin_001",
            "法国零售商在境外收集80万名境外客户的订单信息，传入境内处理后返还法国。"
            "数据血缘及加工范围已确认，处理过程中没有引入境内个人信息或者重要数据。"
            "境内处理者非CIIO，无重要数据，仅判断该批数据返还的出境机制。",
            {"annual_non_sensitive_count": "800000", "destination_region": "法国",
             "exemption_facts": "境外收集产生，境内处理未引入境内个人信息或重要数据。"},
            "新增境外来源数据返还例外，检验是否只看人数而遗漏数据来源与处理范围。",
            ["在已确认来源和处理范围的前提下，讨论第四条免予三种出境机制。"],
            ["仅凭80万人而强制标准合同或认证。", "免予机制等同于免除全部保护义务。"],
            "第四、八、十条",
        ),
        case(
            "holdout_mixed_origin_001",
            "法国订单数据传入境内处理后返还。加工说明表明处理时合并了境内用户画像，"
            "但合并的数据范围、是否可以分离及分别出境的人数尚未核实。非CIIO，无重要数据。"
            "项目经理称所有数据都是法国项目，因此整批应免予。请保留尚未查清的边界。",
            {"destination_region": "法国", "annual_non_sensitive_count": "unknown",
             "exemption_facts": "返还加工中引入境内用户画像，范围及可分离性未核实。"},
            "与境外来源例外组成范围对照，不能将项目名称当成整批数据来源证明。",
            ["不能对整批直接确认第四条例外；说明混合范围及出境口径待核实，保留条件性判断。"],
            ["仅因法国项目认定整批免予。", "擅自设定可分离性、国内人数或明确最终路径。"],
            "第四、七、八条",
            clarification=["引入境内信息的范围、可分离性及相应累计出境口径。"],
        ),
        case(
            "holdout_personal_booking_001",
            "境内平台代20万名个人旅客预订日本酒店，向酒店提供姓名和联系方式。"
            "个人是订房合同当事人，提供的信息仅为履行该个人订房合同所必需，均已确认。"
            "不含敏感个人信息或重要数据，不包含广告推荐或额外画像共享。平台非CIIO。",
            {"annual_non_sensitive_count": "200000", "destination_region": "日本",
             "processing_purpose": "履行个人订房合同",
             "exemption_facts": "个人为合同当事人，向酒店提供的信息确为履约所必需。"},
            "新增个人合同履行必要性例外，区别合同当事人与泛化的商业合同。",
            ["在已确认个人当事人和必要范围内，适用第五条第一项免予机制，并区分剩余义务。"],
            ["无视必要性例外，仅依20万人要求标准合同或认证。", "将免予扩展到额外广告画像。"],
            "第五条第一项、八、十条",
        ),
        case(
            "holdout_business_contract_001",
            "境内广告公司根据与日本分析供应商签订的企业采购合同，向其提供250万名境内用户的"
            "姓名、联系方式及普通营销偏好。累计人数已去重，自2024年1月1日起统计。"
            "个人不是该采购合同当事人，出境不为履行个人合同所必需，无其他免予情形。"
            "非CIIO，无敏感个人信息或重要数据。供应商称签了合同就免予。",
            {"annual_non_sensitive_count": "2500000", "destination_region": "日本",
             "processing_purpose": "商业营销分析",
             "exemption_facts": "企业采购合同，非个人合同履行所必需，无其他免予情形。"},
            "企业服务合同不能替代个人合同例外；检验商业措辞是否误导法律条件判断。",
            ["不能依企业采购合同适用个人合同例外；依已确认普通信息出境规模判断安全评估。"],
            ["仅因签订企业合同就免予三种机制。", "将企业采购合同当成已经完成出境标准合同手续。"],
            "第五条第一项、第七条",
        ),
        case(
            "holdout_nonpersonal_trade_001",
            "设备出口商向德国客户提供机器温度、功率和维修部件型号。经数据分类核对，"
            "该批数据不包含个人信息或重要数据，不能关联到个人，业务属于国际贸易。"
            "仅判断该批设备参数的出境机制，不判断其他业务的数据处理。",
            {"contains_personal_information": False, "annual_non_sensitive_count": "0",
             "destination_region": "德国", "processing_purpose": "设备出口交付",
             "exemption_facts": "国际贸易数据不包含个人信息或重要数据。"},
            "新增数据对象边界：确认为非个人、非重要数据时，不强套个人信息人数机制。",
            ["针对本批数据依第三条讨论免予机制，区分仍存在的数据安全保护义务。"],
            ["没有个人信息仍仅依交易数量要求个人信息出境标准合同。", "将本批免予推广到企业全部业务。"],
            "第三、十一条",
        ),
        case(
            "holdout_pseudonymous_data_001",
            "电商将30万名境内用户的编码化购物记录提供给德国分析商。编码与实名对应表由境内"
            "公司保留，材料和确认填报均明确这些记录仍属于个人信息；不含敏感个人信息。"
            "自2024年1月1日起累计去重出境30万人，非CIIO，无重要数据或特殊免予情形。"
            "销售人员称编码化就属于匿名的非个人数据。",
            {"annual_non_sensitive_count": "300000", "destination_region": "德国",
             "processing_purpose": "客户商业分析", "exemption_facts": "无特殊免予情形。"},
            "与非个人贸易数据形成对象对照，不能用匿名宣传措辞推翻确认事实。",
            ["不能仅依编码化认定非个人数据免予；根据已确认个人信息及人数讨论标准合同或者认证。"],
            ["忽略保留对应关系和确认的个人信息属性，宣称第三条免予。"],
            "第三、八条",
        ),
    ]
