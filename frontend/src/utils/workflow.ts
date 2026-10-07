import type { CaseStatus, ReviewTaskStatus } from '../types/api';

export const CASE_STATUS_LABELS: Record<CaseStatus, string> = {
  draft: '草稿',
  needs_info: '待申报人补充',
  pending_source_verification: '待新法源核验',
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
  waiting_input: '等待补充',
  succeeded: '已完成',
  failed: '运行失败',
  superseded: '已被新提交取代',
};

export const TERMINAL_CASE_STATUSES = new Set<CaseStatus>([
  'approved',
  'conditionally_approved',
  'rejected',
]);

/** Shared prompt so every exit path from the revision editor asks the same question. */
export const REVISION_DISCARD_PROMPT = '文书修改内容尚未提交，关闭后需要重新编辑。确定关闭吗？';

/** Hosts call this before switching views, navigating away, or unmounting the document view. */
export function confirmDiscardRevisionEdit(): boolean {
  return window.confirm(REVISION_DISCARD_PROMPT);
}

/** Unsubmitted annotation text belongs to the paragraph it was written for; moving it would file it under the wrong quote. */
export const ANNOTATION_DISCARD_PROMPT = '当前段落的批注内容尚未保存，切换后会按新段落重新填写。确定切换吗？';

/** The document review calls this before carrying an unsaved annotation over to another paragraph. */
export function confirmDiscardAnnotation(): boolean {
  return window.confirm(ANNOTATION_DISCARD_PROMPT);
}

/** 写批注 and 请 Agent 追查 share one selection: switching modes drops whatever the other box held. */
export const ANNOTATION_MODE_SWITCH_PROMPT = '当前输入尚未保存，切换输入方式后需要重新填写。确定切换吗？';

export function confirmDiscardAnnotationMode(): boolean {
  return window.confirm(ANNOTATION_MODE_SWITCH_PROMPT);
}

/**
 * A host leaving the whole document view only knows that *something* is unsaved — a revision edit or
 * an annotation draft — so its prompt has to name both instead of promising only 文书修改.
 */
export const DOCUMENT_DISCARD_PROMPT = '原文审阅中还有未保存的内容（文书修改或批注），离开后需要重新填写。确定离开吗？';

/** Hosts call this before switching pages, opening another case, or logging out. */
export function confirmDiscardDocumentEdit(): boolean {
  return window.confirm(DOCUMENT_DISCARD_PROMPT);
}
