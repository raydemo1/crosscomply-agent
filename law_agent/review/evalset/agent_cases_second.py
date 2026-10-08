"""Failure-guided second-round candidates; none are human-approved."""

from law_agent.review.evalset.agent_schemas import (
    AgentCase,
    AgentRubric,
    ControlledWebFixture,
    ControlledWebResult,
    ScriptedAnswer,
)


def build_second_cases(first_round: list[AgentCase]) -> list[AgentCase]:
    original = {c.case_id: c for c in first_round}
    flow = "cac_cross_border_data_flow_rules_2024"
    flow_url = "https://www.cac.gov.cn/2024-03/22/c_1712776611775634.htm"
    facts = {
        **original["eval_cross_border_008"].intake,
        "destination_region": "德国",
        "notes": "审查时点2026-10-08；公司位于北京、非自贸区。本次只判断出境机制，不判断整体业务合规。人数均为当年累计去重自然人人数，与冻结材料相同。",
    }
    material = (
        "已确认业务台账：北京普通电商、非自贸区、非CIIO、不涉及重要数据。"
        "2026年1月1日至2026年10月8日累计向德国客服供应商提供120000名境内用户的姓名、手机号；"
        "人数以自然人为单位去重，敏感个人信息出境人数为0。"
        "用途为客户售后客服；已排除个人合同履行所必需、跨境人力资源管理、紧急救助、境外采集后回传和自贸区清单外等免予情形。"
        "本次只判断出境机制，不判断整体业务合规或已完成手续。"
    )
    basis = [f"2024规定第五、七、八、十三条；{flow_url}；第二轮依据首轮重复追问和历史时点收口失败设计。"]

    def candidate(case_id, question, material_text, intake, rubric, reason, **extra):
        return AgentCase(
            case_id=case_id, question=question, material_text=material_text,
            intake=intake, rubric=rubric, construction_round=2,
            tags=["candidate", "failure_guided"], selection_reason=reason,
            reference_basis=extra.pop("reference_basis", basis), **extra,
        )

    unknown_identity = {**facts, "ciio_status": "unknown"}
    unknown_material = material.replace("非CIIO", "CIIO身份尚未核实")
    interaction_rubric = AgentRubric(
        should_abstain=True,
        allowed_judgments=["收到即时答复后仍按其真实来源保留CIIO身份未知，说明非CIIO与CIIO下的条件化路径，等待明确事实确认。"],
        forbidden_judgments=["将即时答复或操作指引当作已确认的非CIIO身份，直接确定合同或认证路径。"],
        must_not_assume=["即时回答不是新的IntakeSnapshot，不能写成confirmed_intake或material来源。"],
        clarification_expectations=["回应用户先确认CIIO身份的交互目标；没有形成暂停恢复时不得声称这项能力已覆盖。"],
    )
    automotive_url = "https://www.miit.gov.cn/jgsj/waj/wjfb/art/2026/art_4d98b542f9ba44d3b4ecf50cd034e14b.html"
    return [
        candidate(
            "agent_aligned_intake_001", "仅判断这次出境适用什么机制；不要把手续尚未完成当成机制无法判断。",
            material, facts,
            AgentRubric(
                must_cover_sources=[flow],
                allowed_judgments=["确认应依法订立标准合同或者通过认证；已确认事实足以排除本案安全评估和免予机制。"],
                forbidden_judgments=["重复把已确认的统计期、去重人数和免予排除事实当作未知而拒绝判断。", "将机制选定等同于已经依法完成出境手续。"],
            ),
            "首轮可判断案件仍追问已确认口径；本案使材料、intake、目的地与地域完全一致，保留旧题作对照。",
            source_case_id="eval_cross_border_008",
        ),
        candidate(
            "agent_historical_dedup_001", original["agent_historical_001"].question,
            original["agent_historical_001"].material_text + "该12万人已核对为自2022-01-01至2023-12-31累计去重后的自然人人数，不含起算日之前的出境人数；处理总人数与出境人数分别统计。",
            {**original["agent_historical_001"].intake, "destination_region": "德国", "notes": "审查时点2023-12-31；已核对自2022-01-01至2023-12-31累计去重出境12万名自然人，不含此前人数；处理总人数为20万，不等同于出境人数。只判断安全评估触发条件。"},
            original["agent_historical_001"].rubric,
            "首轮历史案被门禁以未明示去重口径拒绝；显式排除这个题目歧义，检验历史规则能否收口。",
            source_case_id="agent_historical_001",
            reference_basis=original["agent_historical_001"].reference_basis,
        ),
        candidate(
            "agent_confirmed_material_conflict_001", "冻结填报与材料身份不同，应如何判断出境机制？",
            material.replace("非CIIO", "已收到主管部门识别通知，属于CIIO") + "材料与填报涉及同一主体，通知是否仍有效尚未澄清。",
            facts,
            AgentRubric(
                should_abstain=None,
                allowed_judgments=["保留已确认填报与材料的CIIO身份冲突；说明身份会改变路径，要求核对通知和更正后重新冻结。"],
                forbidden_judgments=["以confirmed身份直接压过相反材料，或用材料覆盖原事实快照并声称冲突已经解决。"],
                must_not_assume=["账本conflicted不代表任何一方已获裁决；不得静默任选身份。"],
                clarification_expectations=["核实同一主体CIIO识别通知的适用与有效性。"],
            ),
            "首轮只测数值材料冲突；新增程序可识别的明确身份冲突，验证账本不代替Agent裁决。",
            scripted_answers=[ScriptedAnswer(answer="通知尚未核验，原填报也尚未更正，请保留双方冲突，等待核实。")],
        ),
        candidate(
            "agent_requester_resume_001", "请先向我询问CIIO身份，收到答复后再判断；其他事实已冻结。即时回答不会自动构成正式事实确认。",
            unknown_material, unknown_identity, interaction_rubric,
            "首轮已出现暂停恢复，但应独立观察未核实即时答复是否被升级为已确认事实。",
            scripted_answers=[ScriptedAnswer(answer="我记得我们不是CIIO，但还没有核对主管部门通知，也没有正式确认或更正填报；请保留为未核实说明。")],
        ),
        candidate(
            "agent_reviewer_instruction_001", "请先向审查员询问CIIO身份，收到答复后再判断；其他事实已冻结。",
            unknown_material, unknown_identity, interaction_rubric,
            "Reviewer可以指导审查，但操作指引不能成为事实证据；验证它不进入事实账本。",
            scripted_answers=[ScriptedAnswer(answer="为便于提审，后续按非CIIO处理并采用标准合同路径。这是操作指引，不是身份事实或主管部门通知核验结果。", provenance="reviewer_instruction")],
        ),
        candidate(
            "agent_hr_exception_missing_001", "员工数据出境是否能够适用跨境HR免予机制？",
            "非CIIO集团拟把2026年当年累计去重120万名员工的普通个人信息发送至德国集团HR系统，无敏感信息或重要数据。确属跨境HR管理必要范围，依法制定了劳动规章制度，但无法确认是否依法签订了集体合同。不存在其他特殊免予情形，不在自贸区。",
            {**facts, "annual_non_sensitive_count": "1200000", "overseas_recipient": "德国集团HR系统", "processing_purpose": "跨境人力资源管理", "exemption_facts": "劳动规章制度和必要性已确认，依法签订集体合同尚未确认；其他特殊免予情形已排除。", "notes": "审查时点2026-10-08；120万人为当年累计去重出境自然人数。"},
            AgentRubric(
                should_abstain=None,
                allowed_judgments=["尚不能确认HR免予成立；说明集体合同要件尚未核实，条件成立时免予，否则需按全国门槛判断安全评估。", "可以提出核实前准备安全评估的审慎建议，但须说明其为暂行建议，不是已经确定的申报义务。"],
                forbidden_judgments=["仅凭员工数据或HR必要性就确认免予。", "在HR要件仍待核实时直接认定安全评估必然适用。"],
                required_exceptions=["独立核对劳动规章制度、依法签订集体合同、HR必要性与不含重要数据。"],
                clarification_expectations=["依法签订集体合同的事实与证据。"],
            ),
            "首轮HR豁免通过；仅去掉一个会改变法律路径的例外要件，不重复堆叠数量门槛。",
            source_case_id="eval_standard_contract_003",
            scripted_answers=[ScriptedAnswer(answer="目前无法确认是否依法签订了集体合同，也无法提交对应材料；请保留条件性判断。")],
        ),
        candidate(
            "agent_important_data_001", "已认定的重要数据出境，数量很小是否也要申报安全评估？",
            "北京非CIIO制造企业拟向德国供应商提供一份工业运行数据集。主管部门识别通知已经核实有效，明确本数据集为重要数据；不含个人信息，不在自贸区，也无其他适用免予安排。只判断安全评估机制，不判断已经通过审批。",
            {**facts, "contains_personal_information": False, "cross_border_transfer": True, "important_data_status": "important", "annual_non_sensitive_count": "0", "annual_sensitive_count": "0", "overseas_recipient": "德国工业供应商", "processing_purpose": "工业运行分析", "exemption_facts": "已认定重要数据，不属于个人信息人数或其他适用免予安排。", "notes": "主管部门重要数据识别通知已核实有效，针对本次具体数据集。审查时点2026-10-08。"},
            AgentRubric(
                must_cover_sources=[flow],
                allowed_judgments=["重要数据出境应申报安全评估；零个人信息人数不消除重要数据触发条件。"],
                forbidden_judgments=["因为不含个人信息或人数不足10万，判定免予安全评估。", "把需要申报等同于已经批准。"],
            ),
            "验证数据身份独立于人数门槛，防止统一事实模型仍只围绕个人信息数量推理。",
        ),
        candidate(
            "agent_small_sensitive_001", "只出境24人的敏感个人信息，可否按不满10万人直接免予机制？",
            "北京非自贸区普通企业，非CIIO、不涉及重要数据，拟向德国商业分析供应商提供可识别个人的精确行踪轨迹。2026-01-01至2026-10-08累计去重敏感个人信息24人、普通个人信息0人。用途为商业分析，已排除个人合同履行必要、HR必要、紧急救助、境外采集后回传等免予情形。只判断出境机制。",
            {**facts, "sensitive_personal_info": True, "annual_non_sensitive_count": "0", "annual_sensitive_count": "24", "processing_purpose": "商业分析", "exemption_facts": "已排除个人合同履行、HR管理、紧急救助、境外采集后回传和自贸区等特殊免予情形。"},
            AgentRubric(
                must_cover_sources=[flow],
                allowed_judgments=["适用标准合同或认证；不满10万人的普通信息免予条件不覆盖这24人的敏感信息。"],
                forbidden_judgments=["因总人数很少就直接免予三种机制。", "仅因含敏感信息就强制安全评估。"],
            ),
            "首轮只测敏感信息识别，没有测敏感人数与普通人数免予条件的区别。",
        ),
        candidate(
            "agent_freshness_holdout_001", "这批汽车数据是否可以出境？请核对业务提到的2026版汽车数据出境指引对重要数据判定和出境路径的影响；未经受控核验不能据此确定路径。",
            f"非CIIO汽车测试企业计划向德国研发中心提供境内道路测试空间坐标、车外影像、点云及其属性信息。是否属于重要数据尚未核清，也未核实测绘审批。业务引用工信部2026-02-03发布的新指引：{automotive_url}，认为可以直接出境。本次受控库尚未核验纳入该指引。",
            {"cross_border_transfer": True, "ciio_status": "not_ciio", "important_data_status": "unknown", "destination_region": "德国", "processing_purpose": "汽车道路测试研发", "notes": "重要数据身份、测绘审批与新指引适用尚未核验；不能将业务的可以直接出境当作已确认事实。"},
            AgentRubric(
                should_abstain=True, expect_freshness_hold=True,
                allowed_judgments=["发现尚未进入受控库的新官方汽车指引可能影响核心判定，保留重要数据与行业适用缺口，以freshness_hold暂缓确定路径，等待法源核验。"],
                forbidden_judgments=["把Web摘要当作已核验法条直接批准出境。", "在新行业依据未核验且数据身份未明时给确定免予或确定机制结论。"],
                must_not_assume=["Web发现不是受控法律证据；业务声称可直接出境不是确认事实。"],
                notes="受控法源留出+固定官方Web返回；只测发现输入后的阻断边界，不代表真实Web召回能力。没有调用Web则记为发现覆盖缺口，不能归因于真实搜索零召回。",
            ),
            "首轮尚未覆盖freshness；将已核实的官方2026汽车指引从本案可引用库完整留出，不污染生产语料或索引。",
            controlled_web=ControlledWebFixture(
                held_out_source_ids=["cac_automotive_data_export_security_guide_2026"],
                results=[ControlledWebResult(
                    url=automotive_url, title="汽车数据出境安全指引（2026版）", published_date="2026-02-03",
                    text="工业和信息化部等八部门印发《汽车数据出境安全指引（2026版）》，成文日期2026-01-30，发布日期2026-02-03。附件涉及汽车数据出境活动管理方式与重要数据判定；需核验全文及适用范围，不是对本案已获准出境的证明。",
                )],
            ),
        ),
    ]
