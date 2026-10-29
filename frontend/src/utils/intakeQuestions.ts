import type { CaseIntake } from '../types/api';

export const INTAKE_QUESTIONS = {
  cross_border_transfer: '是否涉及向境外提供数据？',
  contains_personal_information: '是否包含个人信息？',
  sensitive_personal_info: '是否包含敏感个人信息？（如金融账户、医疗健康、行踪轨迹等）',
  important_data_status: '本次数据是否已被确认为重要数据？',
  ciio_status: '我方是否已被认定为关键信息基础设施运营者？',
  annual_non_sensitive_count: '非敏感个人信息涉及多少人？',
  annual_sensitive_count: '敏感个人信息涉及多少人？',
  count_period: '上述人数按什么期间统计？',
  overseas_recipient: '境外接收方是谁？',
  destination_region: '境外接收地是哪个国家或地区？',
  processing_purpose: '数据出境用于什么业务？',
  legal_basis_or_consent: '已履行哪些告知、同意或其他合规手续？',
  exemption_facts: '本次出境是否涉及个人合同履行、人力资源管理或紧急情况等业务？',
} satisfies Partial<Record<keyof CaseIntake, string>>;

export type IntakeQuestionKey = keyof typeof INTAKE_QUESTIONS;
export type MissingFact = { key: string; reason: string; input_type: 'choice' | 'text' | 'count' | 'exemption' | 'free_text' };

export function intakeQuestion(key: string, period: CaseIntake['count_period'], reason: string): string {
  const question = INTAKE_QUESTIONS[key as IntakeQuestionKey] ?? reason;
  const prefix = period === 'current_year_cumulative' ? '本年度1月1日起累计：'
    : period === 'annual_estimate' ? '全年预计：' : '';
  return key === 'annual_non_sensitive_count' || key === 'annual_sensitive_count' ? prefix + question : question;
}

export function intakeQuestionFields(key: string): IntakeQuestionKey[] {
  if (!Object.prototype.hasOwnProperty.call(INTAKE_QUESTIONS, key)) return [];
  return key === 'annual_non_sensitive_count' || key === 'annual_sensitive_count'
    ? [key, 'count_period'] : [key as IntakeQuestionKey];
}

export function visibleIntakeQuestions(facts: MissingFact[], limit = 4): MissingFact[] {
  const seen = new Set<string>();
  const hasCount = facts.some((fact) => fact.key === 'annual_non_sensitive_count' || fact.key === 'annual_sensitive_count');
  return facts.filter((fact) => {
    if (seen.has(fact.key) || fact.key === 'count_period' && hasCount) return false;
    seen.add(fact.key);
    return true;
  }).slice(0, limit);
}
