import type { Citation, CaseDetailApi, ReviewFacts, RiskLevel } from '../types/api';
import { EMPTY_INTAKE, fromDetail } from '../store/caseStore';

const DATE = '2026-10-26T09:00:00+08:00';
const source: Citation = {
  source_id: 'demo-law-pipl', chunk_id: 'demo-law-pipl-55', title: '中华人民共和国个人信息保护法',
  source_url: 'https://www.cac.gov.cn/2021-08/20/c_1631050028355286.htm',
  citation_ref: '法源-01', citation_label: '《个人信息保护法》第五十五条（节选）',
  article_no: '第五十五条', citation_role: 'primary_legal_basis', can_cite_clause: true,
  usage: 'legal_basis', doc_type: 'law', authority: 'law', law_status: 'effective',
  publish_date: '2021-08-20', effective_date: '2021-11-01', issuing_body: '全国人民代表大会常务委员会', heading_path: ['个人信息处理者的义务'],
  full_article_text: '第五十五条　有下列情形之一的，个人信息处理者应当事前进行个人信息保护影响评估，并对处理情况进行记录：\n（一）处理敏感个人信息；\n（二）利用个人信息进行自动化决策；\n（三）委托处理个人信息、向其他个人信息处理者提供个人信息、公开个人信息；\n（四）向境外提供个人信息；\n（五）其他对个人权益有重大影响的个人信息处理活动。',
};

interface Scenario {
  id: string; title: string; category: string; description: string; risk: RiskLevel;
  question: string; material: string; summary: string; points: string[]; conclusion: string;
  actions: string[]; missing: string[]; types: string[]; purpose: string; recipient: string;
  answer: string;
}

export const scenarios: Scenario[] = [
  {
    id: 'demo-overseas-support', title: '境外客服协作', category: '信息不足', risk: 'insufficient_evidence',
    description: '从材料缺口出发，理解为什么当前不能直接给出出境结论。',
    question: '将工单交给境外客服协作前，还需要核实哪些条件？',
    material: '【虚构示例材料】\n澄川软件计划由境外客服团队协助处理中文售后工单。工单可能包含用户昵称、联系方式及故障截图。境外团队按工单访问数据，暂未明确截图是否包含敏感信息，也未形成接收方权限与删除规则。\n项目负责人尚未提供当年累计涉及人数、个人信息保护影响评估或完整合同安排。以上名称、业务与材料均为演示编写。',
    summary: '当前示例材料不足以确定具体出境路径。应先核实数据范围、累计人数、接收方处理方式及适用条件，再形成有前提的判断。',
    points: ['**数据范围尚未明确**，截图中的信息需要单独核对。', '**累计人数与统计口径缺失**，不能据此选择具体路径。', '接收方权限、保存期限及再提供安排仍待确认。'],
    conclusion: '### 当前判断\n\n**在数据范围、累计人数和接收方安排尚未核实的情况下，不能给出确定的出境路径结论。** 这份示例展示的是如何把待核实事项转化为下一步行动。\n\n### 决定判断的条件\n\n| 核对条件 | 示例现状 | 对判断的影响 |\n| --- | --- | --- |\n| 数据范围 | 工单截图可能包含额外信息 | 先识别字段及敏感信息 |\n| 累计人数 | 尚未提供 | 不能确定具体路径 |\n| 接收方安排 | 权限与删除规则未确定 | 补齐处理边界 |\n\n### 结合本案如何理解\n\n先完成数据清单与事实核对，再评估适用要求。**信息缺失不等于已满足豁免条件。** 个人信息出境的影响评估义务也是需要核查的事项。<sup data-citation-ref="法源-01">①</sup>',
    actions: ['确认工单字段与截图内容，形成最小必要的数据清单。', '核对累计涉及人数、统计期间及接收方处理安排。', '核实影响评估、合同、访问控制与删除机制。'],
    missing: ['截图是否包含敏感个人信息？', '累计人数和统计口径是什么？', '境外接收方能否再提供数据？'],
    types: ['用户昵称', '联系方式', '故障截图'], purpose: '售后工单处理', recipient: '虚构境外客服团队',
    answer: '**这是预先编写的示例回答。**\n\n这段内容的重点在于：**关键前提没有核实，就不能把示例报告理解为确定许可。**\n\n可以沿着三个问题继续核对：\n\n1. 实际传输哪些字段，截图中还有哪些个人信息？\n2. 累计人数按什么期间、什么口径统计？\n3. 境外团队的权限、保存和删除如何约束？\n\n这些内容用于展示报告解释与追问交互，不会写入事实或发起真实审查。',
  },
  {
    id: 'demo-group-hr', title: '集团人事协作', category: '有条件判断', risk: 'medium',
    description: '把处理目的、必要范围和未满足条件放在同一条阅读路径中。',
    question: '集团人事协作中，如何界定必要的数据范围与处理边界？',
    material: '【虚构示例材料】\n栖原科技拟将境内员工的姓名、工作邮箱、岗位与入职日期提供给境外集团人事平台，用于组织管理。材料不包含真实员工信息。\n示例方案约定按岗位分配权限，不默认传输身份证件、家庭成员信息或健康信息。现有材料尚未完整说明人事制度依据、数据保存期限及对员工的告知安排。以上名称、业务与材料均为演示编写。',
    summary: '示例应围绕人事管理的具体目的核对必要性，并保留制度依据、信息范围和接收方安排等前提。仅有“集团内部使用”的说明，还不足以形成完整的合规判断。',
    points: ['**必要性需要落实到具体字段与实际用途**，不能仅依据集团关系推定。', '**制度依据与告知安排仍须核实**，条件应与结论同时阅读。', '对额外敏感信息或用途变化，应重新核对适用条件。'],
    conclusion: '### 当前判断\n\n**在制度依据、处理必要性与接收方安排得到核实后，才能进一步判断适用要求。** 当前示例不对任何具体出境机制作确定判断。\n\n### 按目的核对信息范围\n\n| 处理目的 | 示例字段 | 核对重点 |\n| --- | --- | --- |\n| 组织通讯 | 姓名、工作邮箱 | 是否仅用于工作联系 |\n| 岗位管理 | 岗位、入职日期 | 是否确有管理需要 |\n| 扩展用途 | 未包含 | 新用途应另行核对 |\n\n### 需要继续落实的事项\n\n- 明确人事管理制度与处理依据。\n- 将权限、保存期限和删除责任落实到接收方安排。\n- 核查个人信息保护影响评估等义务。<sup data-citation-ref="法源-01">①</sup>\n\n**示例中的字段收敛只是一个控制措施，不能替代完整的适用分析。**',
    actions: ['逐项核对人事字段是否为实际管理目的所必需。', '补齐制度依据、告知、保存与删除安排。', '记录用途或数据范围变化后的重新核对条件。'],
    missing: ['人事管理制度依据是否明确？', '保存期限及删除机制如何约定？'],
    types: ['姓名', '工作邮箱', '岗位', '入职日期'], purpose: '集团组织与人事管理', recipient: '虚构集团人事平台',
    answer: '**这是预先编写的示例回答。**\n\n“集团内部使用”说明接收关系，**并不自动说明每个字段都有必要，也不能代替对适用条件的核实。**\n\n读这份报告时，可以把三个层次分开看：处理目的是什么；每个字段为什么必要；制度依据、告知与接收方约束是否完整。\n\n如果新增健康、证件等信息或扩展用途，应重新核对。这里展示的是阅读方法，不是对真实业务的审查结论。',
  },
  {
    id: 'demo-aggregate-analytics', title: '经营统计共享', category: '边界清晰', risk: 'low',
    description: '观察一项有明确前提的判断，以及前提变化时如何重新评估。',
    question: '仅共享经营统计汇总表时，判断需要保留哪些前提？',
    material: '【虚构示例材料】\n远汀商贸拟向境外合作方共享按月、按产品类别统计的销量与营收汇总表。表中不包含客户、员工、设备标识或订单明细，也不提供逐人下钻。\n示例说明汇总结果无法识别自然人，也无法结合可获得的其他数据还原个人。该说明尚不代表对重要数据、商业秘密或合同义务作出判断。以上名称、业务与材料均为演示编写。',
    summary: '在汇总数据确实不含可识别自然人的信息、不能还原个人且不提供明细下钻的前提下，可以围绕非个人信息场景继续分析；重要数据、保密与合同要求仍需分别核查。',
    points: ['**不含可识别个人的信息且不能还原个人**，是本示例的决定性前提。', '**匿名化不能仅凭文件名称确认**，需要核对数据与实际使用方式。', '个人信息之外的限制仍然保留，低风险标识不代表无限制使用。'],
    conclusion: '### 当前判断\n\n**本示例仅在数据确实不能识别或还原自然人的前提下，讨论经营汇总信息的共享。** 这不是对所有“统计表”的一概判断。\n\n### 区分两类情形\n\n| 情形 | 数据特征 | 示例阅读方式 |\n| --- | --- | --- |\n| 汇总共享 | 无个人标识，无法还原个人 | 保留并核实这一前提 |\n| 明细下钻 | 可关联订单、设备或个人 | 重新核对个人信息属性 |\n\n### 仍然可见的边界\n\n重要数据、商业秘密、接收方合同及其他限制应分别核对。**如果新增个人标识、可关联明细或提供下钻，原示例前提就不再成立。**',
    actions: ['验证汇总结果与可获得的其他数据是否可能关联个人。', '保持明细与标识字段不对外共享的控制措施。', '单独核对重要数据、商业秘密与合同要求。'],
    missing: [], types: ['月度销量汇总', '分类营收汇总'], purpose: '经营分析', recipient: '虚构境外合作方',
    answer: '**这是预先编写的示例回答。**\n\n这份示例的结论与前提必须一起阅读：**数据确实无法识别或还原个人，并且不提供可关联的明细下钻。**\n\n一旦新增订单标识、设备信息或可关联的明细，便应重新核对原有判断。与此同时，重要数据、商业秘密和合同限制需要分别处理。\n\n“低风险”在这里仅是演示标签，不是对真实数据的许可。',
  },
];

export function exampleDetail(scenario: Scenario): CaseDetailApi {
  const id = scenario.id;
  const facts: ReviewFacts = {
    business_activity: scenario.title, data_types: scenario.types, contains_personal_information: scenario.risk === 'low' ? false : true,
    sensitive_personal_info: null, cross_border_transfer: true, overseas_recipient: scenario.recipient,
    processing_purpose: scenario.purpose, legal_basis_or_consent: null, industry: '虚构示例', regions: [],
    missing_information: scenario.missing, as_of_date: '2026-10-26',
  };
  const citations = scenario.risk === 'low' ? [] : [source];
  const groups = citations.length ? [{ usage: 'legal_basis' as const, citations, scope_note: '公开法条节选，用于演示引用交互；并非完整适用依据。' }] : [];
  const intake = { ...EMPTY_INTAKE, business_activity: scenario.title, data_types: scenario.types, contains_personal_information: facts.contains_personal_information, cross_border_transfer: true, overseas_recipient: scenario.recipient, processing_purpose: scenario.purpose };
  const result = {
    review_result_id: `${id}-result`, review_case_id: id, trace_id: `${id}-trace`, risk_level: scenario.risk,
    decision_summary: scenario.summary, legal_path: null, conclusion: scenario.conclusion, review_facts: facts,
    trigger_reasons: scenario.points, missing_information: scenario.missing, legal_basis_gaps: [],
    recommended_actions: scenario.actions, risk_boundaries: ['本页为虚构案例与预编写示例，未经真实模型审查，不适用于任何实际案件。', '数据范围、目的、接收方或其他前提变化时，需要重新核对。', '本报告不构成正式法律意见。'],
    claims: [], citations, applicable_evidence: groups, issues: [],
  };
  return {
    case: { id, title: scenario.title, question: scenario.question, status: 'pending_review', risk_level: scenario.risk,
      facts_confirmed: true, created_by: 'demo-visitor', owner_id: null, created_at: DATE, updated_at: DATE, has_result: true,
      material_text: scenario.material, material_source: '虚构示例材料', intake, rerank_mode: 'off', trace_id: `${id}-trace`,
      response: { review_case_id: id, trace_id: `${id}-trace`, review_facts: facts, review_result: result,
        evidence_self_check: { status: 'not_checked', second_retrieval_plan: null, triggered_reasons: [], issues: [], second_retrieval_triggered: false },
        citation_groups: groups, second_retrieval_triggered: false, evidence_chunks: [], source_evidence_packets: [] },
    },
    events: [], feedback: null, feedback_entries: [], feishu_approval: null, signed_decision: null, report: null, remediation_plan: null,
    material_snapshot: { id: `${id}-materials`, case_id: id, fingerprint: 'demo-only', version_ids: [`${id}-material-1`], created_by: 'demo-visitor', created_at: DATE },
    intake_snapshot: { id: `${id}-intake`, case_id: id, material_snapshot_id: `${id}-materials`, fingerprint: 'demo-only', intake, created_by: 'demo-visitor', created_at: DATE },
    review_task: { id: `${id}-task`, case_id: id, material_snapshot_id: `${id}-materials`, intake_snapshot_id: `${id}-intake`,
      idempotency_key: id, status: 'succeeded', current_node: null, error_category: null, error_message: null,
      attempt_count: 0, model_id: '示例（未调用模型）', data_boundary_summary: {}, result: {}, agent_state: null,
      steps: [], attempts: [], created_at: DATE, updated_at: DATE },
  };
}

export const exampleCases = scenarios.map((scenario) => fromDetail(exampleDetail(scenario)));
