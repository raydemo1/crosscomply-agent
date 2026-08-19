import type {
  CaseAction,
  CaseEvent,
  CaseIntake,
  Citation,
  CitationGroup,
  DashboardSummaryApi,
  EvidenceSelfCheck,
  FeishuApprovalApi,
  MaterialSnapshotApi,
  ReportRecordApi,
  RetrievalHit,
  ReviewFacts,
  ReviewResponse,
  ReviewTaskApi,
  RuleDecisionApi,
  SourceEvidencePacket,
  WorkbenchUser,
} from '../types/api';
import type { SavedCase } from '../types/case';

/**
 * The public Vercel site is a static product tour. This fixture is deliberately
 * shaped like the persisted API response so the tour exercises the same UI as
 * a real case without exposing a database or an LLM key. The numbers, facts,
 * citations, and events below are authored to stay internally consistent so the
 * demo reads like a genuine conditional-approval review.
 */

const CASE_ID = 'case_demo_cross_border_saas';
const CASE_NUMBER = 'CC-20260818-42AEC816';
const CREATED_AT = '2026-08-18T09:20:00+08:00';
const MATERIAL_FROZEN_AT = '2026-08-18T09:36:00+08:00';
const REVIEW_STARTED_AT = '2026-08-18T09:42:00+08:00';
const REVIEW_FINISHED_AT = '2026-08-18T09:45:26+08:00';
const SECOND_RETRIEVAL_AT = '2026-08-18T09:50:12+08:00';
const APPROVAL_CREATED_AT = '2026-08-18T16:00:00+08:00';
const APPROVED_AT = '2026-08-18T16:42:00+08:00';
const REPORT_AT = '2026-08-18T16:43:00+08:00';

const intake: CaseIntake = {
  business_activity: '企业采购境外 CRM / AI SaaS，用于客户联系人管理、工单协作和客服质量分析',
  data_types: ['客户联系人', '工单内容', '客户服务记录', '设备与登录信息'],
  sensitive_personal_info: true,
  cross_border_transfer: true,
  important_data_status: 'not_important',
  ciio_status: 'not_ciio',
  annual_non_sensitive_count: '182400',
  annual_sensitive_count: '2400',
  overseas_recipient: 'Acme Cloud Europe B.V.（德国法兰克福）',
  destination_region: '欧盟 / 德国',
  processing_purpose: '客户服务、工单协作、服务质量分析和安全运维',
  transfer_mechanism: '个人信息出境标准合同',
  vendor_name: 'NimbusCRM AI（虚构供应商）',
  contract_status: '待完成标准合同签署与备案',
  legal_basis_or_consent: '客户服务关系与隐私告知，敏感个人信息按最小必要原则处理',
  notes: '自由文本工单可能包含客户联系方式和个案描述，已要求供应商限制分处理者访问，具体访问范围仍需落地证据。',
};

const facts: ReviewFacts = {
  business_activity: intake.business_activity,
  data_types: intake.data_types,
  sensitive_personal_info: true,
  cross_border_transfer: true,
  overseas_recipient: intake.overseas_recipient,
  processing_purpose: intake.processing_purpose,
  legal_basis_or_consent: intake.legal_basis_or_consent,
  industry: '制造业客户服务',
  region: '中国境内业务向欧盟供应商提供',
  missing_information: [
    '自由文本工单中敏感个人信息的具体字段范围尚未确认',
    '供应商分处理者名单与远程运维的访问控制证据尚未补齐',
  ],
};

const citations: Citation[] = [
  {
    source_id: 'flk_npc_pipl',
    chunk_id: 'demo-chunk-pipl-38',
    title: '中华人民共和国个人信息保护法',
    source_url: 'https://flk.npc.gov.cn/law-search/search/flfgDetails?bbbs=ff8081817b6472a3017b656cc2040044',
    citation_role: 'primary_legal_basis',
    can_cite_clause: true,
    usage: 'legal_basis',
    citation_label: '《个人信息保护法》第三十八条',
    citation_ref: '法源-01',
    article_no: '第三十八条',
    full_article_text: '个人信息处理者因业务等需要，确需向中华人民共和国境外提供个人信息的，应当具备法定的出境条件，并履行相应的保护义务。',
    doc_type: 'law',
    authority: 'national_law',
    law_status: 'effective',
    publish_date: '2021-08-20',
    effective_date: '2021-11-01',
    issuing_body: '全国人民代表大会常务委员会',
    heading_path: ['第六章 履行个人信息保护职责的部门', '个人信息跨境提供'],
  },
  {
    source_id: 'cac_cross_border_data_flow_rules_2024',
    chunk_id: 'demo-chunk-flow-08',
    title: '促进和规范数据跨境流动规定',
    source_url: 'https://www.cac.gov.cn/2024-03/22/c_1712776611775634.htm',
    citation_role: 'primary_legal_basis',
    can_cite_clause: true,
    usage: 'legal_basis',
    citation_label: '《促进和规范数据跨境流动规定》第八条',
    citation_ref: '法源-02',
    article_no: '第八条',
    full_article_text: '自本规定施行之日起，数据处理者向境外提供个人信息，符合规定情形的，可以免予申报数据出境安全评估、订立个人信息出境标准合同、通过个人信息保护认证。',
    doc_type: 'policy',
    authority: 'ministry_policy',
    law_status: 'effective',
    publish_date: '2024-03-22',
    effective_date: '2024-03-22',
    issuing_body: '国家互联网信息办公室',
    heading_path: ['个人信息出境活动的豁免情形'],
  },
  {
    source_id: 'cac_standard_contract_measures_2023',
    chunk_id: 'demo-chunk-contract-05',
    title: '个人信息出境标准合同办法',
    source_url: 'https://www.cac.gov.cn/2023-02/24/c_1678884830036813.htm',
    citation_role: 'primary_legal_basis',
    can_cite_clause: true,
    usage: 'legal_basis',
    citation_label: '《个人信息出境标准合同办法》第五条',
    citation_ref: '法源-03',
    article_no: '第五条',
    full_article_text: '个人信息处理者向境外提供个人信息，通过订立标准合同方式开展个人信息出境活动的，应当依法开展个人信息保护影响评估，并按照规定订立标准合同。',
    doc_type: 'policy',
    authority: 'ministry_policy',
    law_status: 'effective',
    publish_date: '2023-02-24',
    effective_date: '2023-06-01',
    issuing_body: '国家互联网信息办公室',
    heading_path: ['标准合同订立与个人信息保护影响评估'],
  },
  {
    source_id: 'cac_standard_contract_filing_guide_2024',
    chunk_id: 'demo-chunk-filing-02',
    title: '个人信息出境标准合同备案指南（第二版）',
    source_url: 'https://www.cac.gov.cn/2024-03/22/c_1712783131692707.htm',
    citation_role: 'implementation_reference',
    can_cite_clause: false,
    usage: 'implementation_reference',
    citation_label: '《个人信息出境标准合同备案指南（第二版）》',
    citation_ref: '法源-04',
    article_no: null,
    full_article_text: '备案材料应当能够反映个人信息处理者、境外接收方、处理目的、个人信息规模、保护措施和个人信息保护影响评估等信息。',
    doc_type: 'guideline',
    authority: 'ministry_policy',
    law_status: 'effective',
    publish_date: '2024-03-22',
    effective_date: '2024-03-22',
    issuing_body: '国家互联网信息办公室',
    heading_path: ['备案材料与流程'],
  },
];

const citationGroups: CitationGroup[] = [
  {
    usage: 'legal_basis',
    scope_note: '本案直接决定出境路径的法律依据。',
    citations: citations.slice(0, 3),
  },
  {
    usage: 'implementation_reference',
    scope_note: '用于补充签署、评估和备案的落地要求。',
    citations: citations.slice(3),
  },
];

const evidenceChunks: RetrievalHit[] = citations.map((citation, index) => ({
  chunk_id: citation.chunk_id,
  doc_id: `demo-doc-${index + 1}`,
  source_id: citation.source_id,
  title: citation.title,
  text: citation.full_article_text ?? '',
  score: 0.96 - index * 0.04,
  rank: index + 1,
  retriever: 'hybrid',
  citation_role: citation.citation_role,
  can_cite_clause: citation.can_cite_clause,
  source_url: citation.source_url,
  matched_query_type: index < 3 ? 'legal_issue' : 'missing_information',
  article_no: citation.article_no,
  citation_label: citation.citation_label,
  heading_path: citation.heading_path,
  doc_type: citation.doc_type,
  authority: citation.authority,
  law_status: citation.law_status,
  publish_date: citation.publish_date,
  effective_date: citation.effective_date,
  issuing_body: citation.issuing_body,
  full_article_text: citation.full_article_text,
}));

const sourceEvidencePackets: SourceEvidencePacket[] = citations.map((citation, index) => ({
  source_id: citation.source_id,
  title: citation.title,
  representative_chunk: evidenceChunks[index]!,
  supporting_chunks: [evidenceChunks[index]!],
  neighbor_chunks: [],
}));

const evidenceSelfCheck: EvidenceSelfCheck = {
  status: 'sufficient',
  issues: [],
  triggered_reasons: [
    '首次召回已覆盖全国主路径和标准合同义务',
    '备案实施依据仅命中办法本身，未覆盖备案材料与时限要求，触发二次检索',
  ],
  second_retrieval_triggered: true,
  second_retrieval_plan: {
    expanded_queries: [
      { query_id: 'demo-q-4', query_type: 'legal_issue', text: '个人信息出境标准合同备案材料与时限' },
    ],
    increased_top_k: 12,
    stronger_boost: true,
    reason: '补充核对标准合同签署后的备案动作。',
  },
};

const response: ReviewResponse = {
  review_case_id: CASE_ID,
  trace_id: 'trace_demo_cross_border_saas',
  review_facts: facts,
  review_result: {
    review_result_id: 'result_demo_cross_border_saas',
    review_case_id: CASE_ID,
    trace_id: 'trace_demo_cross_border_saas',
    risk_level: 'medium',
    conclusion:
      '本案属于个人信息出境活动，当前材料显示企业不是关键信息基础设施运营者，且未识别出重要数据<sup data-citation-ref="法源-02">[2]</sup>。按现有业务规模和资料，**可以优先采用个人信息出境标准合同路径**<sup data-citation-ref="法源-03">[3]</sup>，但在标准合同签署、个人信息保护影响评估和备案完成前，不应将客户数据接入生产环境。\n\n需要特别关注的是：自由文本工单可能包含敏感个人信息<sup data-citation-ref="法源-01">[1]</sup>，供应商的分处理者、远程运维地点和删除/备份机制仍需形成可验证的合同与技术证据，相关落地要求可参考备案指南<sup data-citation-ref="法源-04">[4]</sup>。',
    review_facts: facts,
    trigger_reasons: [
      '向德国境外接收方提供客户联系人、工单和服务记录',
      '年度个人信息主体规模约 18.24 万，敏感个人信息主体约 2400 人',
      '自由文本工单存在敏感信息进入境外 SaaS 的可能',
    ],
    missing_information: facts.missing_information,
    recommended_actions: [
      '完成个人信息保护影响评估，覆盖数据范围、处理目的、接收方、分处理者和安全措施。',
      '使用官方范本签署个人信息出境标准合同，并明确供应商变更、协助履约和责任承担。',
      '补齐分处理者清单、远程运维地点、访问权限和会话日志证据。',
      '建立自由文本敏感信息提示、抽检、最小化和删除流程，避免无关信息出境。',
      '完成标准合同备案并把备案材料、评估报告和合同版本归入案件档案。',
    ],
    risk_boundaries: [
      '若后续识别出重要数据、关键信息基础设施运营者身份或规模达到安全评估阈值，应重新判断路径。',
      '若供应商新增分处理者、改变处理地点或扩大处理目的，应触发变更复审。',
      '本结论基于当前确认事实，不替代法务对合同、评估和备案材料的最终确认。',
    ],
    claims: [
      {
        text: '当前材料更适合先走个人信息出境标准合同路径。',
        supporting_chunk_ids: ['demo-chunk-pipl-38', 'demo-chunk-flow-08', 'demo-chunk-contract-05'],
        supporting_citation_refs: ['法源-01', '法源-02', '法源-03'],
      },
      {
        text: '标准合同签署前仍需完成个人信息保护影响评估，并在上线前补齐备案与供应商控制证据。',
        supporting_chunk_ids: ['demo-chunk-contract-05', 'demo-chunk-filing-02'],
        supporting_citation_refs: ['法源-03', '法源-04'],
      },
    ],
    citations,
    applicable_evidence: citationGroups,
  },
  evidence_self_check: evidenceSelfCheck,
  citation_groups: citationGroups,
  second_retrieval_triggered: true,
  retrieval_queries: [
    { query_id: 'demo-q-1', query_type: 'legal_issue', text: '个人信息出境标准合同适用条件与评估义务' },
    { query_id: 'demo-q-2', query_type: 'material_fact', text: '境外 CRM 工单联系人 敏感个人信息 分处理者' },
    { query_id: 'demo-q-3', query_type: 'region_condition', text: '中国企业向德国提供个人信息 合规路径' },
    { query_id: 'demo-q-4', query_type: 'legal_issue', text: '个人信息出境标准合同备案材料与时限' },
  ],
  evidence_chunks: evidenceChunks,
  source_evidence_packets: sourceEvidencePackets,
};

const materialSnapshot: MaterialSnapshotApi = {
  id: 'snapshot_demo_cross_border_saas',
  case_id: CASE_ID,
  fingerprint: 'd3c9a12f4b7e90cd51a2b8f6e7d03c4a9b1e2f5a8c7d0e3f6b9a2c5d8e1f4b7a',
  version_ids: ['material_demo_application', 'material_demo_dpa', 'material_demo_inventory'],
  created_by: 'user_demo_reviewer',
  created_at: MATERIAL_FROZEN_AT,
};

const ruleDecision: RuleDecisionApi = {
  id: 'rules_demo_cross_border_saas',
  case_id: CASE_ID,
  material_snapshot_id: materialSnapshot.id,
  ruleset_version: 'cn-cross-border-main-path-2026.08',
  facts: {
    cross_border_transfer: true,
    is_ciio: false,
    important_data: false,
    contains_personal_information: true,
    contains_sensitive_personal_information: true,
    cumulative_personal_information_subjects: 182400,
    cumulative_sensitive_personal_information_subjects: 2400,
    claimed_exemption: null,
    exemption_facts_confirmed: null,
    special_regimes: [],
  },
  determination: {
    status: 'determined',
    rule_version: 'cn-cross-border-main-path-2026.08',
    candidate_paths: [
      { code: 'standard_contract_or_certification', label: '个人信息出境标准合同或认证', confidence: 'determined', reason: '当前事实未触发安全评估阈值，标准合同与认证均需结合材料和法源进一步确认。' },
      { code: 'security_assessment', label: '数据出境安全评估', confidence: 'possible', reason: '若重要数据、关基身份或出境规模达到阈值，应重新评估。' },
    ],
    needs_info: [
      { key: 'sensitive_fields_scope', reason: '需确认自由文本工单中敏感个人信息字段的具体范围。' },
      { key: 'subprocessor_evidence', reason: '需确认供应商分处理者名单与远程运维的访问控制证据。' },
    ],
    rule_hits: [
      { rule_id: 'national-path-not-ciio', summary: '当前材料确认申请企业不是关键信息基础设施运营者。', basis_ids: ['flk_npc_pipl'] },
      { rule_id: 'national-path-no-important-data', summary: '当前材料未识别出重要数据，未触发数据出境安全评估申报。', basis_ids: ['cac_cross_border_data_flow_rules_2024'] },
    ],
    official_bases: [
      { basis_id: 'flk_npc_pipl', title: '中华人民共和国个人信息保护法', article: '第三十八条', issuing_body: '全国人民代表大会常务委员会', source_url: citations[0].source_url },
      { basis_id: 'cac_cross_border_data_flow_rules_2024', title: '促进和规范数据跨境流动规定', article: '第八条', issuing_body: '国家互联网信息办公室', source_url: citations[1].source_url },
      { basis_id: 'cac_standard_contract_measures_2023', title: '个人信息出境标准合同办法', article: '第五条', issuing_body: '国家互联网信息办公室', source_url: citations[2].source_url },
    ],
    requires_rag_human_confirmation: true,
    manual_confirmation_reasons: [
      '需要核对自由文本工单的敏感信息范围',
      '需要确认供应商分处理者和远程运维地点',
    ],
  },
  created_at: MATERIAL_FROZEN_AT,
};

const reviewTask: ReviewTaskApi = {
  id: 'task_demo_cross_border_saas',
  case_id: CASE_ID,
  material_snapshot_id: materialSnapshot.id,
  rule_snapshot_id: ruleDecision.id,
  idempotency_key: 'demo-idempotency-key',
  status: 'succeeded',
  current_node: 'report_ready',
  error_category: null,
  error_message: null,
  attempt_count: 1,
  model_id: 'deepseek-v4-pro · 企业批准审查模型',
  data_boundary_summary: { deployment: 'enterprise-approved-api', model_endpoint: '批准模型端点' },
  result: { review_result_id: response.review_result.review_result_id },
  attempts: [{
    attempt_number: 1,
    worker_id: 'worker-demo',
    status: 'succeeded',
    failed_node: null,
    error_category: null,
    error_message: null,
    started_at: REVIEW_STARTED_AT,
    finished_at: REVIEW_FINISHED_AT,
  }],
  created_at: REVIEW_STARTED_AT,
  updated_at: SECOND_RETRIEVAL_AT,
};

const feishuApproval: FeishuApprovalApi = {
  id: 'approval_demo_cross_border_saas',
  case_id: CASE_ID,
  task_id: reviewTask.id,
  provider: 'feishu',
  instance_id: 'fi_demo_cross_border_saas_001',
  status: 'conditionally_approved',
  approver_name: '林律师（法务审核人）',
  decided_at: APPROVED_AT,
  payload: { decision: 'conditionally_approved', note: '完成 ACT-001、ACT-002、ACT-004、ACT-005 后方可接入生产数据。' },
  created_at: APPROVAL_CREATED_AT,
  updated_at: APPROVED_AT,
};

const report: ReportRecordApi = {
  id: 'report_demo_cross_border_saas',
  case_id: CASE_ID,
  approval_id: feishuApproval.id,
  object_key: 'demo/cross-border-saas/decision-report.pdf',
  sha256: '9a4f6c2d8e1b3a5c7f0d2e4b6a8c0d1e3f5a7b9c2d4f6a8b0c1e3d5f7a9b2c4d',
  metadata: { source: 'public demonstration fixture', generated_at: REPORT_AT },
  created_at: REPORT_AT,
};

const events: CaseEvent[] = [
  { id: 'event-demo-01', case_id: CASE_ID, actor_id: 'user_demo_reviewer', event_type: 'case_created', from_status: null, to_status: 'draft', payload: { intake_versions: 1 }, created_at: CREATED_AT },
  { id: 'event-demo-02', case_id: CASE_ID, actor_id: 'user_demo_reviewer', event_type: 'status_changed', from_status: 'draft', to_status: 'pending_review', payload: { material_versions: 3, snapshot_id: materialSnapshot.id }, created_at: MATERIAL_FROZEN_AT },
  { id: 'event-demo-03', case_id: CASE_ID, actor_id: 'worker-demo', event_type: 'review_started', from_status: 'pending_review', to_status: 'review_running', payload: { attempt: 1, rule_version: ruleDecision.ruleset_version }, created_at: REVIEW_STARTED_AT },
  { id: 'event-demo-04', case_id: CASE_ID, actor_id: 'worker-demo', event_type: 'review_completed', from_status: 'review_running', to_status: 'pending_feishu_approval', payload: { second_retrieval: true, evidence_status: 'sufficient', citations: 4 }, created_at: SECOND_RETRIEVAL_AT },
  { id: 'event-demo-05', case_id: CASE_ID, actor_id: 'user_demo_reviewer', event_type: 'status_changed', from_status: 'pending_feishu_approval', to_status: 'conditionally_approved', payload: { provider: 'feishu', approver: feishuApproval.approver_name, instance_id: feishuApproval.instance_id }, created_at: APPROVED_AT },
  { id: 'event-demo-06', case_id: CASE_ID, actor_id: 'user_demo_reviewer', event_type: 'action_created', from_status: null, to_status: null, payload: { count: 5, blocking: 4 }, created_at: APPROVED_AT },
  { id: 'event-demo-07', case_id: CASE_ID, actor_id: 'worker-demo', event_type: 'report_generated', from_status: null, to_status: null, payload: { report_id: report.id, object_key: report.object_key }, created_at: REPORT_AT },
];

const actions: CaseAction[] = [
  { id: 'action-demo-01', case_id: CASE_ID, title: '签署个人信息出境标准合同', description: '使用官方范本完成企业与境外接收方签署，并留存最终版本。', owner_role: '法务', priority: 'high', status: 'in_progress', due_date: '2026-08-25', created_at: APPROVED_AT, updated_at: APPROVED_AT },
  { id: 'action-demo-02', case_id: CASE_ID, title: '完成个人信息保护影响评估', description: '覆盖处理目的、数据范围、接收方、分处理者和安全措施。', owner_role: '隐私合规', priority: 'high', status: 'open', due_date: '2026-08-25', created_at: APPROVED_AT, updated_at: APPROVED_AT },
  { id: 'action-demo-03', case_id: CASE_ID, title: '补齐分处理者与远程运维证据', description: '确认分处理者名单、运维国家/地区、权限和会话日志控制。', owner_role: '安全团队', priority: 'high', status: 'open', due_date: '2026-08-29', created_at: APPROVED_AT, updated_at: APPROVED_AT },
  { id: 'action-demo-04', case_id: CASE_ID, title: '建立自由文本敏感信息治理', description: '上线提示、抽检、最小化和删除流程，减少无关敏感信息进入工单。', owner_role: '业务负责人', priority: 'medium', status: 'in_progress', due_date: '2026-09-05', created_at: APPROVED_AT, updated_at: APPROVED_AT },
  { id: 'action-demo-05', case_id: CASE_ID, title: '完成标准合同备案', description: '整理合同、影响评估和备案材料，形成可回溯的归档记录。', owner_role: '法务', priority: 'medium', status: 'open', due_date: '2026-09-12', created_at: APPROVED_AT, updated_at: APPROVED_AT },
];

export const DEMO_USER: WorkbenchUser = {
  id: 'user_demo_reviewer',
  username: 'demo@crosscomply.local',
  display_name: '公开演示访客',
  role: 'requester',
};

export const DEMO_CASE: SavedCase = {
  id: CASE_ID,
  traceId: response.trace_id,
  savedAt: REPORT_AT,
  question: `境外 CRM / AI SaaS 上线前，向欧洲供应商提供客户联系人和工单数据，是否可以采用个人信息出境标准合同？（${CASE_NUMBER}）`,
  materialText: '华辰智造拟采购托管于德国的 NimbusCRM AI，用于客户联系人管理、客服工单、服务质量分析和安全运维。材料显示年度向境外提供约 18.24 万名个人信息主体的数据，敏感个人信息主体约 2400 人。自由文本工单可能出现客户联系方式和个案描述，供应商声明会使用德国区域并保留分处理者和远程运维能力。',
  materialSource: 'cross-border-saas-demo-materials',
  response,
  status: 'conditionally_approved',
  intake,
  actions,
  events,
  feedback: null,
  materialSnapshot,
  ruleDecision,
  reviewTask,
  feishuApproval,
  signedDecision: { ...feishuApproval, id: 'decision_demo_cross_border_saas' },
  report,
};

export const DEMO_SUMMARY: DashboardSummaryApi = {
  total_cases: 1,
  status_counts: {
    draft: 0,
    needs_info: 0,
    pending_review: 0,
    review_running: 0,
    pending_feishu_approval: 0,
    approved: 0,
    conditionally_approved: 1,
    rejected: 0,
    run_failed: 0,
  },
  risk_counts: { medium: 1, high: 0, low: 0, insufficient_evidence: 0 },
  recent_cases: [],
};
