# Fact Provenance 完成后的 Agent 候选基线

## 审计结论

事实补充、明确确认、同材料新事实快照重审、来源展示与案件解释问答已经形成闭环。代码检查、现有测试与本次真实生产runtime评测支持继续建立能力基线；没有发现必须先新增产品功能才能开展评测的缺口。这不等于完成线上验收或法律人工审定。

当前17个案例全部是合成候选，未人工审定。首轮8案实跑后，依据重复追问、历史统计口径与来源边界的真实表现增加9案。没有将retrieval召回标签直接当成法律判断答案，也没有规定搜索、阅读和法律推理顺序。两个交互案例明确提出先询问身份的用户目标，观察实际暂停恢复。

## 修复前后证据

| 生产版本 | 候选PASS | 候选FAIL | 未完成评测 | 预算耗尽 |
| --- | --- | --- | --- | --- |
| Fact Provenance完成后、收口修复前 | 11 | 2 | 4 | 0 |
| 法源关系与审查范围修复后 | 16 | 1 | 0 | 1 |

**不是法律正确率。** 相同17个输入与相同语料，每案每版本仅运行一次；模型具有随机性，judge与受测Agent同为`deepseek-flash`，这种对照不能证明未知案件上的泛化或精确因果效果。修复后的16个PASS中，有1个只是预算耗尽后的系统abstain，不能视为正常完成。另1案存在judge与候选约束的分歧，必须人工审定。

[逐案对照](../data/review_runs/agent_core_20261009_comparison/report.md)保存两版结果链接；[机器可读汇总](../data/review_runs/agent_core_20261009_comparison/summary.json)保存计数、运行目录和版本变化。已校验：两版输入与rubric一致、语料散列一致，每版各运行目录的生产代码散列一致，各次运行期间没有生产变更。该17案基线对应当时的生产版本；后续两案修复与不同版本的定向复测见下节，不与原基线合并计算。

修复前运行分为[首轮8案](../data/review_runs/agent_core_20261008_post_provenance_round1/report.assessed.md)、[两案口径对照](../data/review_runs/agent_core_20261009_round2_controls/report.assessed.md)和[七案来源及适用边界](../data/review_runs/agent_core_20261009_round2_boundaries/report.assessed.md)。修复后分为[首轮8案](../data/review_runs/agent_core_20261009_fixed_round1/report.assessed.md)、[三案针对性复测](../data/review_runs/agent_core_20261009_fixed_controls/report.assessed.md)和[六案边界复测](../data/review_runs/agent_core_20261009_fixed_boundaries/report.assessed.md)。分目录是为了先检验修复，再完成其余案例；没有挑选多次运行中的最好结果。

## 已定位并修复的问题

### 新旧法源条件被叠加

修复前`agent_aligned_intake_001`的材料和intake完全一致：当前年累计去重12万人普通个人信息，非CIIO、非重要数据，特殊免予情形已排除。Agent先提出标准合同或认证判断，但语义校验要求再满足旧标准合同办法的“自上年1月1日起累计不满10万人”，Agent接受该反馈并向用户追问。

这忽略了[2024年规定第十三条](https://www.cac.gov.cn/2024-03/22/c_1712776611775634.htm)对不一致旧规定的衔接。它不是召回不到法源，而是法源关系处理错误及校验反馈被过度接受。24人敏感信息对照也出现类似旧、新统计期间叠加，最终abstain。

修复没有编码人数、法条编号或法律路径。Agent与verifier改为依据审查时点、适用范围及法条中的衔接或修改规定判断关系；不能机械叠加冲突条件，也不能仅凭较新日期废弃整个旧法源。Agent仍须依据原文复核语义反馈，最终引用和语义门禁继续生效。

修复后，完全一致的12万人对照、24人敏感信息对照均交付标准合同或认证的候选判断；对应引用、语义校验和评测judge记录完整保存。

### 已确认口径被重新列为未知，审查范围混淆

历史案例正确选用了2023-12-31时点的法源，但重复质疑已说明的统计起点。第二轮再明确累计期间、自然人去重与不包含此前人数，仍追问相同内容。普通机制选择也被影响评估、同意或手续完成情况阻塞。

verifier现在明确接收`review_goal`和被引法源的效力、发布时间、生效时间信息。与材料一致且没有具体矛盾的确认填报，可支持明确限定前提的法律判断；它仍不表示客观独立核实或已经完成手续。机制选择、整体合规与手续完成按目标区分，报告自行增加的法律断言仍须有依据。未知字段、真实矛盾和会改变本题结论的缺口没有被放宽。

修复后两个历史案例均形成候选安全评估判断。[2022年办法第四条](https://www.cac.gov.cn/2022-07/07/c_1658811536396503.htm)及其生效时点是本题候选依据；没有追溯套用2024门槛。

## 17案基线暴露的 bad cases

### HR免予要件未确认：交付与评分存在分歧

`agent_hr_exception_missing_001`已确认HR必要性、劳动规章制度、当年累计120万人等，但依法签订集体合同尚未确认。候选约束允许条件性判断，禁止把“要件尚未确认”直接转成确定的免予不成立及安全评估必然适用。

修复后报告主结论为“不能适用跨境HR免予，应当申报安全评估”，同时在边界中说明集体合同若已签订，判断可能改变。当前候选约束将其列为FAIL；judge却接受该保守适用普通规则的推理，给PASS。这是需要复核的真实分歧，不能仅凭自动评分宣称报告可靠，也不能直接将候选约束升格为法律真理。应人工核对普通规则与例外的证明要求、条件性合规建议及报告主结论的确定程度。没有为消除分歧而修改题目或增加生产字段判断规则。

[原交付](../data/review_runs/agent_core_20261009_fixed_boundaries/agent_hr_exception_missing_001/payload.json)与[judge及确定性评分](../data/review_runs/agent_core_20261009_fixed_boundaries/agent_hr_exception_missing_001/result.json)均保留。

### 填报与材料冲突：报告类型表达失败

`agent_confirmed_material_conflict_001`正确保留CIIO与非CIIO双方，账本也保留两条`conflicted`来源。然而Agent把该跨来源冲突作为`material_conflict`提交；现有报告类型要求两段不同材料原文，而本案另一方来自确认填报。连续6次finish收到相同结构错误，最终16轮预算耗尽，系统abstain。

本案没有错误给出确定路径，judge接受其最终保留判断，但运行质量不合格。失败分类为报告类型/来源表达、反馈修正能力与预算耗尽。下一次修复应让报告正确表达跨来源冲突，并避免无效结构的重复提交；不应伪造第二段材料或放松来源验证来提高分数。

[完整checkpoint与提交错误](../data/review_runs/agent_core_20261009_fixed_boundaries/agent_confirmed_material_conflict_001/state.json)及[可读诊断](../data/review_runs/agent_core_20261009_fixed_boundaries/report.assessed.md)保留。PASS不能掩盖这一问题。

## 两案判断指导与报告反馈修复

定向复核发现，HR旧报告第8步曾被生产校验器以“集体合同未知但主结论确定”拒绝，第9步只重读同一材料，第10步却通过；没有新增事实消解缺口。这不仅是候选rubric与judge的分歧，也反映了语义校验的不稳定。

本次仅改善Agent、verifier和judge对条件未知、条件不满足、暂行准备建议的理解，并核对主结论与条件说明是否一致；没有编码人数、HR条件或法律路径选择。输出schema说明跨来源冲突可用现有`missing_information`表达，结构错误提供可执行的修正反馈，材料引用验证没有放宽。

两个候选的`should_abstain`改为`null`，按原有允许/禁止判断评估条件性报告，不强制`risk_level=insufficient_evidence`；HR另明确允许标明性质的暂行准备建议。没有judge时，这种语义评分不形成总评。问题、材料、intake及预设回答均与旧记录一致，所有原始结果保留，不能将rubric调整前后的分数当作同一指标。

| 定向运行 | 实际结果 | 执行表现 |
| --- | --- | --- |
| [判断指导修复后的HR](../data/review_runs/agent_cases_20261009_guidance/agent_hr_exception_missing_001/payload.json) | 尚不能确认免予，也不能认定必然不成立；条件不满足时才讨论申报；`legal_path=null` | 10轮完成，无追问，无预算耗尽，候选judge通过 |
| [判断指导修复后的冲突案](../data/review_runs/agent_cases_20261009_guidance/report.md) | 未再触发原有冲突类型校验错误，但摘要中的`not_ciio`被误认作Markdown | 7次格式拒绝，16轮预算耗尽，候选judge未通过 |
| [摘要格式修复后的冲突案](../data/review_runs/agent_cases_20261009_guidance_formatfix/agent_confirmed_material_conflict_001/payload.json) | 保留双方冲突，条件性解释两条路径；一次类型错误后依据反馈改为`missing_information` | 11轮正常完成，无预算耗尽，候选judge通过 |

摘要误判来自两个重复的纯文本检查将任何下划线视作格式标记。现在复用相同的格式验证，允许词内标识符下划线，仍拒绝Markdown强调、反引号及多段摘要。新增回归覆盖该真实摘要和原有格式限制。

最后两条成功记录属于不同生产快照，仅说明对应原始问题在定向运行中得到改善，不合并为新的17案基线。运行期间生产散列未改变，语料散列与原基线一致。两个成功案例都没有实际暂停恢复，不能用它们证明交互覆盖。冲突案当次来源账本只登记了填报的CIIO身份，材料相反值未入账；工作事实使用`under_review`，报告保留双方。该缺口的后续修复见下节。

更新后的judge对[HR旧报告的回放](../data/review_runs/agent_cases_20261009_guidance/judge_replay_hr_old_v2.json)判为不通过，识别了“未知→不成立”和主结论与边界不一致。第一次回放也识别该推理，但误把澄清期望理解为强制追问；保留[首次回放](../data/review_runs/agent_cases_20261009_guidance/judge_replay_hr_old.json)，随后明确澄清可以通过报告披露及核实建议完成。回放仅调用评测judge，不重新运行Agent。相关输入与原报告摘要均保存。

最终全量pytest：597通过，21个既存警告；本次涉及文件Ruff与diff-check通过。真实复测只涉及上述两个候选，同模型judge和单次样本仍不能替代人工法律审定。

## 材料观察独立于工作事实

漏记原因是材料来源值直接取自综合判断后的`ReviewFacts`：当Agent为保留身份争议把工作事实设为`under_review`时，材料的明确陈述就无法独立入账。现在`record_facts(facts, material_facts)`分别接收工作事实与材料观察；每条观察只包含现有业务字段和陈述值，程序绑定材料快照并维护来源、状态与去重。字段值复用现有事实类型校验，没有新增法律判断字段或推理规则。

旧的`material_fact_fields`接口已经删除，不保留旧参数、回退或迁移逻辑；人工输入和案件对话缺少明确来源时拒绝继续，不再补造旧输入的来源标记。

仅对[原冲突案例再做一次真实复测](../data/review_runs/agent_case_20261009_ledger_values/report.md)：12轮正常完成，无预算耗尽，工作身份保持`under_review`；账本保留`not_ciio / confirmed_intake / conflicted`及`ciio / material / conflicted`两条记录，报告以`missing_information`保留争议和核实事项。问题、材料、intake和预设回答未变；运行期间生产散列未变。不与此前17案结果合并，也不宣称暂停恢复已覆盖。

检查结果：全量pytest 602通过，21个既存警告；前端9项测试与构建通过。提交涉及文件Ruff、diff-check通过；全仓Ruff仍有9项不在本次改动中的既存问题。本次提交包含事实确认/新快照重审、来源展示、案件问答和候选评测建设，原始模型运行产物保存在被Git忽略的`data/review_runs/`，不随源码推送。

## 事实来源与freshness边界

真实运行中，申请人即时回答以`applicant_statement/unverified`进入账本；Reviewer指引保留在交互步骤，没有成为账本事实；CIIO直接身份冲突保留双方；最终payload包含账本。修复前后都实际产生了暂停恢复，不能再仅用无模型框架测试代替该能力的观察。

freshness案例完整留出`cac_automotive_data_export_security_guide_2026`的69个chunk，评测专用库剩1763个；生产ES/pgvector索引保持1832条。现有工具会过滤专用库之外的服务命中，读取也不能访问留出来源。Web返回固定的[官方发布信息](https://www.miit.gov.cn/jgsj/waj/wjfb/art/2026/art_4d98b542f9ba44d3b4ecf50cd034e14b.html)，不直接成为法条证据，两版均保留核心判断并形成freshness hold。它只证明受控发现输入下的阻断表现，不证明真实Web检索的召回能力。具体语料与fixture保存在对应案例目录。

## 评测工具修正与验收边界

- judge只接收最终交付、账本、实际引用依据及题目约束，不接收生产语义判词或未引用检索结果。
- manifest记录生产Python模块、评测文件、语料与法源manifest散列；冻材料版本使用真实正文摘要，不再写全零摘要。
- 暂停后缺少预设回答计为UNEVALUATED。首轮原始两案曾记FAIL，原文件没有覆盖；另存`assessment.json`与`report.assessed.md`解释离线分类修正，没有重新调用模型或改变法律评分。
- 认证、余额或明确限流故障停止后续模型调用；本次新基线没有遇到该阻断。
- 全量pytest：584通过，21个既存警告；前端9项测试与构建通过。此次涉及文件Ruff与diff-check通过；全量Ruff仍有16项既存问题，没有扩大清理范围。

人工审定仍是成为Golden的必要步骤，尤其是HR的收口分歧、剩余同意义务、地方清单适用及历史题的事实前提。本轮已完成候选Eval Set、真实生产基线、授权的通用判断修复与相同案例复测，尚不能宣称拥有人审真实案件Golden准确率。

## 后续产品提升顺序

优先解决本次已暴露的判断收口分歧与跨来源冲突报告表达。其次可以考虑：正式报告在新事实重审前后的变化对照、法源核验完成后在案件页明确提示受影响结论与复审动作、在现有只读案件解释之外提供显式的补充调查入口。当前案件问答仅使用该版本冻结材料和已引用证据，并有上下文容量限制，不能冒充具备额外法域调查能力。这些功能都不需要先于本轮基线建设。
