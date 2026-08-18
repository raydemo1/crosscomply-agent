import type {
  CaseDetailApi,
  ComplianceFactsApi,
  FreezeMaterialSnapshotResponse,
  ReviewTaskApi,
} from './api';

const facts: ComplianceFactsApi = {
  cross_border_transfer: true,
  is_ciio: false,
  important_data: false,
  contains_personal_information: true,
  contains_sensitive_personal_information: true,
  cumulative_personal_information_subjects: 100,
  cumulative_sensitive_personal_information_subjects: 10,
  claimed_exemption: null,
  exemption_facts_confirmed: null,
  special_regimes: [],
};

export const frozenInputsFixture = {
  material_snapshot: {
    id: 'material_snapshot_1',
    case_id: 'case_1',
    fingerprint: 'a'.repeat(64),
    version_ids: ['material_version_1'],
    created_by: 'user_1',
    created_at: '2026-08-18T00:00:00Z',
  },
  rule_decision: {
    id: 'rule_snapshot_1',
    case_id: 'case_1',
    material_snapshot_id: 'material_snapshot_1',
    ruleset_version: 'national-path-2026-01',
    facts,
    determination: {
      status: 'determined',
      rule_version: 'national-path-2026-01',
      candidate_paths: [{ code: 'standard_contract_or_certification', label: '标准合同或认证', confidence: 'determined', reason: '低于安全评估阈值' }],
      needs_info: [],
      rule_hits: [],
      official_bases: [],
      requires_rag_human_confirmation: false,
      manual_confirmation_reasons: [],
    },
    created_at: '2026-08-18T00:00:00Z',
  },
} satisfies FreezeMaterialSnapshotResponse;

export const reviewTaskFixture = {
  id: 'review_task_1',
  case_id: 'case_1',
  material_snapshot_id: 'material_snapshot_1',
  rule_snapshot_id: 'rule_snapshot_1',
  idempotency_key: 'b'.repeat(64),
  model_id: 'enterprise-model',
  data_boundary_summary: { deployment: 'enterprise-approved-api' },
  status: 'queued',
  current_node: null,
  error_category: null,
  error_message: null,
  attempt_count: 0,
  result: null,
  attempts: [],
  created_at: '2026-08-18T00:00:00Z',
  updated_at: '2026-08-18T00:00:00Z',
} satisfies ReviewTaskApi;

export const detailEnterpriseFixture = {
  material_snapshot: frozenInputsFixture.material_snapshot,
  rule_decision: frozenInputsFixture.rule_decision,
  review_task: reviewTaskFixture,
  feishu_approval: {
    id: 'approval_1',
    case_id: 'case_1',
    task_id: 'review_task_1',
    provider: 'feishu',
    instance_id: 'instance_1',
    status: 'pending',
    approver_name: null,
    decided_at: null,
    payload: {},
    created_at: '2026-08-18T00:00:00Z',
    updated_at: '2026-08-18T00:00:00Z',
  },
  signed_decision: null,
  report: {
    id: 'report_1',
    case_id: 'case_1',
    approval_id: 'approval_1',
    object_key: 'cases/case_1/reports/report.pdf',
    sha256: 'c'.repeat(64),
    metadata: { material_snapshot_id: 'material_snapshot_1' },
    created_at: '2026-08-18T00:00:00Z',
  },
} satisfies Pick<
  CaseDetailApi,
  | 'material_snapshot'
  | 'rule_decision'
  | 'review_task'
  | 'feishu_approval'
  | 'signed_decision'
  | 'report'
>;
