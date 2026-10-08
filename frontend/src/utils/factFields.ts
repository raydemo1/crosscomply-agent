import type { CaseIntake, FactLedgerEntryApi } from '../types/api';
import type { SavedCase } from '../types/case';

export type FactField = keyof Pick<CaseIntake,
  'business_activity' | 'data_types' | 'contains_personal_information' |
  'sensitive_personal_info' | 'cross_border_transfer' | 'overseas_recipient' |
  'processing_purpose' | 'legal_basis_or_consent' | 'important_data_status' | 'ciio_status' |
  'annual_non_sensitive_count' | 'annual_sensitive_count' | 'count_period' |
  'destination_region' | 'exemption_facts'>;

export const FACT_FIELDS: Record<FactField, { label: string; options?: Array<[string, string]>; kind?: 'boolean' | 'list' }> = {
  business_activity: { label: '业务活动' },
  data_types: { label: '涉及的数据', kind: 'list' },
  contains_personal_information: { label: '包含个人信息', kind: 'boolean' },
  sensitive_personal_info: { label: '包含敏感个人信息', kind: 'boolean' },
  cross_border_transfer: { label: '存在数据出境', kind: 'boolean' },
  overseas_recipient: { label: '境外接收方' },
  processing_purpose: { label: '处理目的' },
  legal_basis_or_consent: { label: '法律基础或同意情况' },
  important_data_status: { label: '重要数据情况', options: [['unknown', '尚不确定'], ['not_important', '已确认为非重要数据'], ['important', '已确认为重要数据'], ['under_review', '正在核实']] },
  ciio_status: { label: '关键信息基础设施运营者身份', options: [['unknown', '尚不确定'], ['not_ciio', '非关键信息基础设施运营者'], ['ciio', '关键信息基础设施运营者'], ['under_review', '正在核实']] },
  annual_non_sensitive_count: { label: '非敏感个人信息人数' },
  annual_sensitive_count: { label: '敏感个人信息人数' },
  count_period: { label: '人数统计期间', options: [['unknown', '尚未确认'], ['current_year_cumulative', '当年累计'], ['annual_estimate', '全年估算'], ['other', '其他统计口径']] },
  destination_region: { label: '数据接收地' },
  exemption_facts: { label: '可能适用豁免的业务事实' },
};

export function factLabel(field: string): string {
  if (field === 'supplemental_statement') return '补充说明';
  return FACT_FIELDS[field as FactField]?.label ?? ({ industry: '行业', regions: '境内地区', notes: '备注', followup_answers: '补充回答', transfer_mechanism: '申报人拟采用路径', vendor_name: '供应商', contract_status: '合同情况' } as Record<string, string>)[field] ?? '补充事实';
}

export function factValue(field: string, value: unknown): string {
  if (value === null || value === undefined || value === '' || value === 'unknown') return '尚未确认';
  if (typeof value === 'boolean') return value ? '是' : '否';
  const option = FACT_FIELDS[field as FactField]?.options?.find(([key]) => key === value);
  if (option) return option[1];
  if (Array.isArray(value)) return value.join('、') || '尚未确认';
  if (typeof value === 'object') return Object.values(value).map(String).join('；');
  return String(value);
}

export function valuesChanged(values: Partial<Record<FactField, unknown>>, intake: CaseIntake): boolean {
  return Object.entries(values).some(([field, value]) => JSON.stringify(value) !== JSON.stringify(intake[field as FactField]));
}

export function caseFactLedger(saved: SavedCase): FactLedgerEntryApi[] {
  const task = saved.reviewTask;
  if (!task || saved.materialSnapshot?.id !== task.material_snapshot_id || saved.intakeSnapshot?.id !== task.intake_snapshot_id) return [];
  const ledger = task.agent_state?.fact_ledger ?? task.result?.fact_ledger as FactLedgerEntryApi[] | undefined ?? [];
  const statements = saved.events
    .filter((e) => e.event_type === 'matter_agent_turn' && e.payload.task_id === task.id && e.payload.input_provenance === 'applicant_statement')
    .flatMap((e) => Object.entries((e.payload.reply as { proposed_facts?: Record<string, unknown> } | undefined)?.proposed_facts ?? {})
      .map(([field, value]): FactLedgerEntryApi => ({ field, value, source_type: 'applicant_statement', status: 'unverified', source_ref: e.id })));
  return [...ledger, ...statements];
}
