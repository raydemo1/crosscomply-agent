import { useEffect, useRef, type JSX } from 'react';
import type { SavedCase } from '../types/case';
import { createFeishuApproval, retryReviewTask, runCase, waitForReviewTask } from '../api/client';
import { openCase } from '../store/caseStore';
import AgentQuestionPanel from './AgentQuestionPanel';

interface CaseWorkflowActionsProps {
  saved: SavedCase;
  canManage: boolean;
  allowApplicantAnswer?: boolean;
  operation: string | null;
  error: string | null;
  setOperation: (value: string | null) => void;
  setError: (value: string | null) => void;
  /** Opens the material editing flow so the user can answer by uploading or updating materials. */
  onEditMaterial?: () => void;
  compact?: boolean;
  focusAnswer?: boolean;
  onAnswerFocused?: () => void;
}

export default function CaseWorkflowActions({
  saved,
  canManage,
  allowApplicantAnswer = false,
  operation,
  error,
  setOperation,
  setError,
  onEditMaterial,
  compact = false,
  focusAnswer,
  onAnswerFocused,
}: CaseWorkflowActionsProps): JSX.Element | null {
  const answerRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!focusAnswer || saved.reviewTask?.status !== 'waiting_input' || !answerRef.current) return;
    answerRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' });
    answerRef.current.focus({ preventScroll: true });
    onAnswerFocused?.();
  }, [focusAnswer, saved.reviewTask?.status, onAnswerFocused]);
  const canAnswerAgent = allowApplicantAnswer && saved.reviewTask?.status === 'waiting_input';
  const guidedActionSyncFailed = hasUnresolvedGuidedActionSyncFailure(saved);
  if (!canManage && !canAnswerAgent) return null;

  const execute = async (name: string, action: () => Promise<void>): Promise<void> => {
    setOperation(name);
    setError(null);
    try {
      await action();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '流程操作失败');
    } finally {
      setOperation(null);
    }
  };

  const startReview = (): void => {
    void execute('run', async () => {
      const queued = await runCase(saved.id);
      await openCase(saved.id);
      await waitForReviewTask(queued.task_id, async () => {
        await openCase(saved.id);
      });
      await openCase(saved.id);
    });
  };

  const retryReview = (): void => {
    const taskId = saved.reviewTask?.id;
    if (!taskId) return;
    void execute('retry', async () => {
      const retried = await retryReviewTask(taskId);
      await openCase(saved.id);
      await waitForReviewTask(retried.id, async () => {
        await openCase(saved.id);
      });
      await openCase(saved.id);
    });
  };

  const createApproval = (): void => {
    void execute('approval', async () => {
      await createFeishuApproval(saved.id);
      await openCase(saved.id);
    });
  };

  const activeTask = Boolean(saved.reviewTask && ['queued', 'running', 'waiting_input'].includes(saved.reviewTask.status));
  // POST /api/cases/{id}/run rejects a re-run when the latest task already succeeded on the currently frozen
  // inputs, so the button is only offered while the frozen input has not been investigated yet.
  const frozenInputAlreadyReviewed = saved.reviewTask !== null
    && saved.reviewTask.status === 'succeeded'
    && saved.materialSnapshot !== null
    && saved.reviewTask.material_snapshot_id === saved.materialSnapshot.id
    && saved.reviewTask.intake_snapshot_id === saved.intakeSnapshot?.id;
  const hasAction = canAnswerAgent || (canManage && (saved.status === 'pending_review'
    || (saved.status === 'needs_info' && !activeTask)
    || saved.status === 'run_failed'
    || saved.status === 'pending_source_verification'
    || saved.status === 'pending_feishu_approval'
    || saved.reviewTask?.status === 'waiting_input'));
  if (!hasAction && !error) return null;

  if (saved.reviewTask?.status === 'waiting_input') {
    return <div id="case-agent-answer" className="case-answer-area" ref={answerRef} tabIndex={-1}>
      <AgentQuestionPanel saved={saved} requester={allowApplicantAnswer} busy={operation !== null} execute={execute} onEditMaterial={onEditMaterial} />
      {error ? <p className="case-answer-area__error" role="alert">{error}</p> : null}
    </div>;
  }

  const controls = (
    <div className="workflow-actions__controls">
      {canManage && saved.status === 'pending_review' ? (activeTask
        ? <span>{saved.reviewTask?.status === 'queued' ? '审查任务已进入队列，等待 Worker 执行，无需手动启动。' : 'Agent 正在审查当前材料，完成后会自动更新结论。'}</span>
        : <button type="button" className="case-header__action-btn case-header__action-btn--accent" disabled={operation !== null} onClick={startReview}>{operation === 'run' ? '正在入队…' : '重新进入审查队列'}</button>) : null}
      {canManage && saved.status === 'needs_info' && !activeTask ? (frozenInputAlreadyReviewed
        ? <span>当前冻结材料与事实已完成调查，请申报人补充材料或事实后重新提交，或由申报人发起整案复核。</span>
        : <button type="button" className="case-header__action-btn case-header__action-btn--accent" disabled={operation !== null} onClick={startReview}>{operation === 'run' ? '调查启动中…' : '按最新材料重新调查'}</button>) : null}
      {canManage && saved.status === 'pending_source_verification' ? <span>{saved.events.some((event) => event.event_type === 'knowledge_recheck_pending') ? '官方法源已完成核验，案件待人工复核；原结论未自动改写' : '发现可能影响结论的新官方法源，正在核验；原结论不会自动改写'}</span> : null}
      {canManage && saved.status === 'run_failed' ? <button type="button" className="case-header__action-btn case-header__action-btn--accent" disabled={operation !== null || !saved.reviewTask} onClick={retryReview}>{operation === 'retry' ? (guidedActionSyncFailed ? '正在恢复整改清单…' : '重新运行中…') : (guidedActionSyncFailed ? '重试整改清单保存' : '重试失败任务')}</button> : null}
      {canManage && saved.status === 'pending_feishu_approval' && !saved.feishuApproval ? <button type="button" className="case-header__action-btn case-header__action-btn--accent" disabled={operation !== null} onClick={createApproval}>{operation === 'approval' ? '正在创建审批…' : '发起飞书审批'}</button> : null}
    </div>
  );

  if (compact) {
    return <div className="case-header__workflow">{controls}{error ? <div className="case-header__workflow-error" role="alert">{error}</div> : null}</div>;
  }

  return (
    <section className="card workflow-actions" aria-label="审核流程操作">
      <div className="workflow-actions__copy">
        <span>{canManage ? '审核人操作' : '申报人补充'}</span>
        <strong>{canAnswerAgent ? 'Agent 等待申报人补充信息' : workflowActionTitle(saved)}</strong>
        <small>{canAnswerAgent ? '请直接回答下方问题；回答会进入本次审查过程。' : workflowActionHint(saved)}</small>
      </div>
      {controls}
      {error ? <div className="workflow-actions__error" role="alert">{error}</div> : null}
    </section>
  );
}

function workflowActionTitle(saved: SavedCase): string {
  if (saved.status === 'pending_source_verification') return '最新官方法源核验与案件复核';
  if (saved.status === 'pending_review') {
    if (saved.reviewTask?.status === 'queued') return '审查任务排队中';
    if (saved.reviewTask?.status === 'running' || saved.reviewTask?.status === 'waiting_input') return 'Agent 正在审查';
    return '材料已提交，等待进入审查队列';
  }
  if (saved.status === 'run_failed') return hasUnresolvedGuidedActionSyncFailure(saved)
    ? '审查结果已保存，整改清单生成失败'
    : '失败记录已保留，可以人工重试';
  if (saved.status === 'pending_feishu_approval') return saved.feishuApproval ? '飞书审批已发起，等待权威回写' : '审查已完成，可以发起飞书审批';
  return '流程状态已更新';
}

function workflowActionHint(saved: SavedCase): string {
  if (saved.status === 'pending_source_verification') return saved.events.some((event) => event.event_type === 'knowledge_recheck_pending') ? '官方法源已完成核验，待负责人复核当前案件。' : '核验期间不发起最终审批。';
  if (saved.status === 'pending_review') {
    if (saved.reviewTask?.status === 'queued') return '任务已进入队列，等待 Worker 执行，无需手动启动。';
    if (saved.reviewTask?.status === 'running' || saved.reviewTask?.status === 'waiting_input') return 'Agent 正在调查材料、检索法源并核验证据，完成后自动更新结论。';
    return '正常提交会自动进入审查队列；若任务未开始，可由审核人重新入队。';
  }
  if (saved.status === 'run_failed') return hasUnresolvedGuidedActionSyncFailure(saved)
    ? '可以单独重试整改清单保存，不会重新运行审查模型。'
    : `失败节点：${saved.reviewTask?.current_node || '未记录'}；重试不会覆盖历史尝试。`;
  if (saved.status === 'pending_feishu_approval') return '最终通过、退回或撤回状态仅接受飞书验签事件。';
  return '流程状态已更新。';
}

function hasUnresolvedGuidedActionSyncFailure(saved: SavedCase): boolean {
  if (saved.status === 'run_failed' && saved.reviewTask?.status === 'succeeded') return true;
  const taskId = saved.reviewTask?.id;
  if (!taskId) return false;
  const syncEvents = saved.events.filter((event) =>
    ['guided_action_list_failed', 'guided_action_list_reconciled'].includes(event.event_type)
    && event.payload.task_id === taskId,
  );
  return syncEvents[syncEvents.length - 1]?.event_type === 'guided_action_list_failed';
}
