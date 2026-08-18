import type { CaseStatus, ReviewTaskStatus } from '../types/api';

export const CASE_STATUS_LABELS: Record<CaseStatus, string> = {
  draft: '草稿',
  needs_info: '待补件',
  pending_review: '待审查',
  review_running: '审查运行中',
  pending_feishu_approval: '待飞书审批',
  approved: '已通过',
  conditionally_approved: '附条件通过',
  rejected: '已退回',
  run_failed: '运行失败',
};

export const REVIEW_TASK_STATUS_LABELS: Record<ReviewTaskStatus, string> = {
  queued: '排队中',
  running: '运行中',
  succeeded: '已完成',
  failed: '运行失败',
};

export const TERMINAL_CASE_STATUSES = new Set<CaseStatus>([
  'approved',
  'conditionally_approved',
  'rejected',
]);
