import type { RemediationTaskApi, WorkbenchUser } from '../types/api';

export function actionableTasks(tasks: RemediationTaskApi[], user: WorkbenchUser): RemediationTaskApi[] {
  return tasks.filter((task) => {
    if (task.is_current === false) return false;
    if (task.phase === 'post_approval' && !['approved', 'conditionally_approved'].includes(task.case_status ?? '')) return false;
    const assigned = task.assignee_id === user.id || task.assignee?.id === user.id;
    const submission = task.submissions?.[task.submissions.length - 1];
    const ownWork = assigned && ['open', 'in_progress'].includes(task.status);
    const review = user.role !== 'requester' && task.status === 'pending_review' && Boolean(submission) && submission?.submitted_by !== user.id;
    return ownWork || review;
  });
}
