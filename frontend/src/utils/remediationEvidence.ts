import type { RemediationAssessmentApi, RemediationSubmissionApi } from '../types/api';

export function evidenceStanding(submission: RemediationSubmissionApi | null | undefined, assessment: RemediationAssessmentApi | null | undefined): string {
  if (!submission?.evidence?.some((item) => item.kind === 'file' || item.kind === 'case_material')) {
    return '仅有申报人声明，尚未提交证明材料';
  }
  if (!assessment || assessment.run_status === 'running') {
    return '证明材料已提交，待核验';
  }
  if (assessment.run_status !== 'completed') return '证明材料已提交，本次核验尚未完成';
  if (!assessment.status) return '证明材料已提交，尚无核验结论';
  if (assessment.status === 'resolved') return '已核验整改落实情况，待审核人确认';
  if (assessment.status === 'insufficient_evidence') return '现有材料尚不足以证明整改已落实，待人工复核';
  return '核验发现待处理事项，请查看核验意见';
}
