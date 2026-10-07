import { useEffect, useRef, type ReactNode } from 'react';
import type { CaseStatus, RemediationPlanApi, ReviewTaskStatus, UserRole } from '../types/api';
import './CaseProgress.css';

const CASE_STAGES = [
  ['提交申请', '提供材料'],
  ['Agent 审查', '形成结论'],
  ['补充核实', '确认缺口'],
  ['人工审批', '人工审批'],
  ['案件归档', '保存记录'],
] as const;

/**
 * How the progress action behaves: locate a task in the list, start the re-review,
 * or reveal the answer form the Agent is waiting on.
 */
export type ProgressActionKind = 'list' | 'rereview' | 'answer';

interface ProgressState {
  current: number;
  owner: string;
  status: string;
  guidance: string;
  actionLabel?: string;
  actionKind?: ProgressActionKind;
}

export interface CaseProgressProps {
  status: CaseStatus;
  viewerRole: UserRole;
  remediationPlan?: RemediationPlanApi | null;
  /** Runtime state of the review task, used to tell "排队中" apart from "正在审查". */
  reviewTaskStatus?: ReviewTaskStatus | null;
  /** Whether a Feishu approval instance already exists for this case. */
  approvalStarted?: boolean;
  onOpenAction?: (kind: ProgressActionKind) => void;
  /** Hosts with no inline answer form send the user to the real one instead of revealing this card. */
  onAnswerAction?: () => void;
  /** Hosts set this when navigation has to land the reader on the answer form, not the top of the page. */
  revealAnswer?: boolean;
  onAnswerRevealed?: () => void;
  actionSlot?: ReactNode;
  children?: ReactNode;
}

function activeTasks(plan?: RemediationPlanApi | null) {
  return plan?.tasks.filter((task) => task.is_current !== false) ?? [];
}

function progressState(
  status: CaseStatus,
  viewerRole: UserRole,
  plan?: RemediationPlanApi | null,
  reviewTaskStatus?: ReviewTaskStatus | null,
  approvalStarted?: boolean,
): ProgressState {
  const tasks = activeTasks(plan);
  const preApprovalTasks = tasks.filter((task) => task.phase === 'pre_approval');
  const openTasks = preApprovalTasks.filter((task) => task.status === 'open' || task.status === 'in_progress');
  const pendingReview = preApprovalTasks.filter((task) => task.status === 'pending_review');
  const totalOpen = openTasks.length + pendingReview.length;

  if (['approved', 'conditionally_approved', 'rejected'].includes(status)) {
    return { current: 4, owner: '系统归档', status: '最终决定已留痕', guidance: '可查看正式报告、最终决定和完整审计记录。' };
  }
  if (status === 'pending_feishu_approval') {
    return {
      current: 3,
      owner: '审核人 / 企业审批',
      status: approvalStarted ? '审批中' : '待发起审批',
      guidance: approvalStarted
        ? '已发起飞书审批，等待审批结果；最终决定以飞书验签事件为准。'
        : '送审前核实已完成，等待审核人发起飞书审批。',
    };
  }
  if (status === 'needs_info') {
    // An Agent waiting for an answer is the most immediate step: the applicant still has to reply before
    // any re-review makes sense, so this has to be decided before the remediation list is considered.
    if (reviewTaskStatus === 'waiting_input') {
      return {
        current: 2,
        owner: viewerRole === 'requester' ? '申报人' : '审核人',
        status: '待回答 Agent 问题',
        guidance: 'Agent 正在等待补充信息，回答后会自动继续本次审查，无需重新发起复核。',
        actionLabel: '回答 Agent 问题',
        actionKind: 'answer',
      };
    }
    // guided-rereview only refuses unfinished *blocking* pre-approval tasks, so eligibility follows blocking
    // items; non-blocking ones stay visible as unfinished but must not hide the re-review entry.
    const blockingOpen = openTasks.filter((task) => task.blocking);
    const blockingPending = pendingReview.filter((task) => task.blocking);
    if (viewerRole !== 'requester' && blockingPending.length > 0) {
      return { current: 2, owner: '审核人', status: `${blockingPending.length} 项等待验收`, guidance: '核验申报人补充内容；确认后即可继续整案复核。', actionLabel: '打开待复核项', actionKind: 'list' };
    }
    if (blockingOpen.length > 0) {
      if (viewerRole !== 'requester') {
        return { current: 2, owner: '申报人', status: `${blockingOpen.length} 项待申报人处理`, guidance: '等待申报人确认业务事实或说明措施情况，提交后再进行核验。', actionLabel: '查看处理清单', actionKind: 'list' };
      }
      return { current: 2, owner: '申报人', status: `${blockingOpen.length} 项待核实`, guidance: '确认业务事实，或说明措施是否适用、是否已完成。提交后由 Agent 核验，必要时交审核人确认。', actionLabel: '进入处理清单', actionKind: 'list' };
    }
    if (blockingPending.length > 0) {
      return { current: 2, owner: 'Agent / 审核人', status: `${blockingPending.length} 项待核验`, guidance: '申报人已提交，正在等待 Agent 或审核人核验。' };
    }
    if (plan && preApprovalTasks.length > 0) {
      const unfinishedNote = totalOpen > 0 ? `，仍有 ${totalOpen} 项非阻塞事项未完成` : '';
      // Only the case's own applicant may call guided-rereview; reviewers would get a 404 from the API.
      if (viewerRole === 'requester') {
        return { current: 2, owner: '申报人', status: '可发起整案复核', guidance: `阻塞性事项已处理${unfinishedNote}，可按最新材料重新审查。`, actionLabel: '发起整案复核', actionKind: 'rereview' };
      }
      return { current: 2, owner: '申报人', status: '等待申报人发起复核', guidance: `阻塞性事项已处理${unfinishedNote}，由申报人按最新材料发起整案复核。` };
    }
    return { current: 2, owner: '申报人', status: '等待补充信息', guidance: '补充所缺事实后，Agent 会继续核验并更新结论。', actionLabel: plan ? '查看处理清单' : undefined, actionKind: 'list' };
  }
  if (status === 'review_running') {
    const queued = reviewTaskStatus === 'queued';
    return {
      current: 1,
      owner: 'Agent',
      status: queued ? '排队中' : '正在审查',
      guidance: queued ? '审查任务已进入队列，等待执行，当前无需操作。' : 'Agent 正在调查、检索法源并核验证据，当前无需操作。',
    };
  }
  if (status === 'pending_source_verification') {
    return { current: 1, owner: 'Agent / 审核人', status: '正在核验官方法源', guidance: '发现可能影响结论的新官方材料，核验完成前不进入最终审批。' };
  }
  if (status === 'run_failed') {
    return { current: 1, owner: '审核人', status: '审查未完成', guidance: '失败记录已保留；修复后可从本次任务继续重试。' };
  }
  if (status === 'pending_review') {
    if (reviewTaskStatus === 'queued' || reviewTaskStatus === 'running') {
      const queued = reviewTaskStatus === 'queued';
      return { current: 0, owner: 'Agent', status: queued ? '排队中' : '正在审查', guidance: queued ? '审查任务已进入队列，等待执行，当前无需操作。' : 'Agent 正在审查当前材料，当前无需操作。' };
    }
    return { current: 0, owner: '系统', status: '材料已提交', guidance: '申请材料和基本事实已保存，提交后会自动进入审查队列。' };
  }
  return { current: 0, owner: '申报人', status: '待提交', guidance: '补齐材料和基本事实后提交申请。' };
}

export default function CaseProgress({ status, viewerRole, remediationPlan, reviewTaskStatus, approvalStarted, onOpenAction, onAnswerAction, revealAnswer, onAnswerRevealed, actionSlot, children }: CaseProgressProps): JSX.Element {
  const state = progressState(status, viewerRole, remediationPlan, reviewTaskStatus, approvalStarted);
  const remediationRequired = status === 'needs_info' || activeTasks(remediationPlan).some((task) => task.phase === 'pre_approval');
  const detailsRef = useRef<HTMLDetailsElement>(null);
  const actionsRef = useRef<HTMLDivElement>(null);

  const revealAnswerForm = (): void => {
    // The answer form sits inside the collapsed body, so reveal and scroll to it rather than leaving the page.
    if (detailsRef.current) detailsRef.current.open = true;
    window.requestAnimationFrame(() => actionsRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' }));
  };

  const handleAction = (kind: ProgressActionKind): void => {
    // There is no answer form on this host, so the only useful thing is to route the user to one.
    if (kind === 'answer' && onAnswerAction) { onAnswerAction(); return; }
    if (kind === 'answer') { revealAnswerForm(); return; }
    onOpenAction?.(kind);
  };

  useEffect(() => {
    if (!revealAnswer) return;
    if (detailsRef.current) detailsRef.current.open = true;
    const frame = window.requestAnimationFrame(() => {
      actionsRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' });
      onAnswerRevealed?.();
    });
    return () => window.cancelAnimationFrame(frame);
  }, [revealAnswer, onAnswerRevealed]);

  return (
    <details className="card case-progress" aria-label="案件进度" ref={detailsRef}>
      <summary className="case-progress__summary">
        <span className="case-progress__summary-copy"><small>案件进度</small><strong>{CASE_STAGES[state.current][0]}</strong></span>
        <span className="case-progress__status"><b>{state.owner}</b><span>{state.status}</span></span>
        <span className="case-progress__head">
          {state.actionLabel && (onOpenAction || onAnswerAction || state.actionKind === 'answer') ? <button type="button" className="btn-primary case-progress__primary" onClick={(event) => { event.preventDefault(); event.stopPropagation(); handleAction(state.actionKind ?? 'list'); }}>{state.actionLabel}</button> : null}
          <span className="case-progress__toggle" aria-hidden="true" />
        </span>
      </summary>
      <div className="case-progress__body">
        <ol className="case-progress__steps">
          {CASE_STAGES.map(([title, caption], index) => {
            const skipped = index === 2 && !remediationRequired && state.current > index;
            const phase = index < state.current ? (skipped ? 'is-conditional' : 'is-done') : index === state.current ? 'is-current' : '';
            return <li className={phase} key={title} aria-current={index === state.current ? 'step' : undefined}>
              <span className="case-progress__index">{index < state.current && !skipped ? '✓' : index + 1}</span>
              <div><strong>{title}</strong><small>{skipped ? '按需进入' : caption}</small></div>
            </li>;
          })}
        </ol>
        <div className="case-progress__current">
          <p className="case-progress__guidance">{state.guidance}</p>
          {actionSlot ? <div className="case-progress__actions" ref={actionsRef}>{actionSlot}</div> : null}
        </div>
        {children ? <div className="case-progress__supplement">{children}</div> : null}
      </div>
    </details>
  );
}