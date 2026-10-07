import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CaseKnowledgeRecheckApi, CaseStatus, Citation, CitationGroup, RetrievalHit, ReviewApiResponse, ReviewFacts, ReviewIssue, UserRole } from '../types/api';
import { isReviewFailedResponse } from '../types/api';
import type { CitationVerdict, SavedCase } from '../types/case';
import { setCitationVerdict } from '../store/caseStore';
import { openCase } from '../store/caseStore';
import { answerReviewTask, caseReportDownloadUrl, createFeishuApproval, getCaseKnowledgeRechecks, retryReviewTask, runCase, waitForReviewTask } from '../api/client';
import RiskBadge from './RiskBadge';
import CitationList from './CitationList';
import FeedbackPanel from './FeedbackPanel';
import GroundedClaims, { cssId } from './GroundedClaims';
import MarkdownText from './MarkdownText';
import ShareCaseDialog from './ShareCaseDialog';
import type { RevisionSelection } from './RevisionWorkspace';
import DocumentReview from './DocumentReview';
import CaseProgress from './CaseProgress';
import { downloadHtml, downloadMarkdown } from '../utils/report';
import { CASE_STATUS_LABELS, REVIEW_TASK_STATUS_LABELS, confirmDiscardDocumentEdit } from '../utils/workflow';
import './RemediationPlanPage.css';
import {
  EVIDENCE_ISSUE_LABELS,
  EVIDENCE_STATUS_BADGE_CLASS,
  EVIDENCE_STATUS_LABELS,
  AUTHORITY_LABELS,
  CITATION_ROLE_LABELS,
  ISSUE_KIND_LABELS,
  citationDisplayLabel,
  DOC_TYPE_LABELS,
  QUERY_TYPE_LABELS,
  USAGE_LABELS,
  formatTime,
  relativeTime,
  renderBool,
  renderList,
  renderText,
  shortId,
} from '../utils/display';

interface CaseDetailPageProps {
  saved: SavedCase;
  canEdit: boolean;
  onEdit: (saved: SavedCase) => void;
  /** Called when the user wants to start a fresh review from this case's inputs. */
  onRerun: (question: string, material: string) => void;
  /** Called when the user wants to go back to the workbench. */
  onBack: () => void;
  /** Reviewers and admins can maintain persisted remediation actions. */
  canManageActions: boolean;
  viewerRole: UserRole;
  /** Navigation host opens the independent remediation-plan page. */
  onOpenRemediationPlan?: () => void;
  /** Set by the host when arriving here must land on the Agent question rather than the report top. */
  focusAgentAnswer?: boolean;
  onAgentAnswerFocused?: () => void;
  /** Reports unsubmitted revision or annotation text so the navigation host can guard its own routes out. */
  onDocumentDirtyChange?: (dirty: boolean) => void;
}

type SavedCaseWithResponse = SavedCase & { response: ReviewApiResponse };

/** Ordered facts shown in the 材料事实摘要 grid. */
const FACT_FIELDS: Array<{ key: string; label: string; render: (f: ReviewFacts) => string }> = [
  { key: 'business_activity', label: '业务活动', render: (f) => renderText(f.business_activity) },
  { key: 'cross_border_transfer', label: '跨境传输', render: (f) => renderBool(f.cross_border_transfer) },
  { key: 'overseas_recipient', label: '境外接收方', render: (f) => renderText(f.overseas_recipient) },
  { key: 'data_types', label: '数据类型', render: (f) => renderList(f.data_types) },
  { key: 'sensitive_personal_info', label: '敏感个人信息', render: (f) => renderBool(f.sensitive_personal_info) },
  { key: 'processing_purpose', label: '处理目的', render: (f) => renderText(f.processing_purpose) },
  { key: 'legal_basis', label: '法律依据/同意', render: (f) => renderText(f.legal_basis_or_consent) },
  { key: 'regions', label: '地区', render: (f) => renderList(f.regions) },
  { key: 'industry', label: '行业', render: (f) => renderText(f.industry) },
  { key: 'as_of_date', label: '法源适用日期', render: (f) => f.as_of_date || '审查当天' },
  { key: 'missing_information', label: '缺失信息', render: (f) => renderList(f.missing_information) },
];

const DISCLAIMER_SENTENCE_DETECTOR = /本结论基于当前材料(?:和|及|、)\s*已召回证据[，,、]?\s*\**不构成正式法律意见\**[。；;]?/;
const CONCLUSION_DISCLAIMER_SENTENCE = /本结论基于当前材料(?:和|及|、)\s*已召回证据[，,、]?\s*\**不构成正式法律意见\**[。；;]?/g;
const TRAILING_CITATION_NOTE = /(?:^|\n)\s*引用说明[：:][^\n]*(?=\n|$)/g;
const LAW_STATUS_LABELS: Record<string, string> = {
  effective: '现行有效',
  not_yet_effective: '尚未生效',
  amended: '已修订，需核对适用版本',
  repealed: '已废止',
  unknown: '效力待核验',
};

function cleanConclusionForDisplay(value: string, removeWhenBoundaryAlreadyShows: boolean): string {
  let cleaned = value
    .replace(TRAILING_CITATION_NOTE, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim();
  const disclaimerMatches = [...cleaned.matchAll(CONCLUSION_DISCLAIMER_SENTENCE)];
  if (disclaimerMatches.length > 1 || (removeWhenBoundaryAlreadyShows && disclaimerMatches.length > 0)) {
    const last = disclaimerMatches[disclaimerMatches.length - 1];
    const start = last.index ?? -1;
    if (start >= 0) {
      cleaned = `${cleaned.slice(0, start)}${cleaned.slice(start + last[0].length)}`
        .replace(/\n{3,}/g, '\n\n')
        .trim();
    }
  }
  return cleaned;
}

export default function CaseDetailPage({
  saved,
  canEdit,
  onEdit,
  onRerun,
  onBack,
  canManageActions,
  viewerRole,
  onOpenRemediationPlan,
  focusAgentAnswer,
  onAgentAnswerFocused,
  onDocumentDirtyChange,
}: CaseDetailPageProps): JSX.Element {
  const [workflowOperation, setWorkflowOperation] = useState<string | null>(null);
  const [workflowError, setWorkflowError] = useState<string | null>(null);
  const [detailView, setDetailView] = useState<'document' | 'report' | 'records'>('report');
  const [documentTarget, setDocumentTarget] = useState<RevisionSelection | null>(null);
  const [reportIssueTarget, setReportIssueTarget] = useState<string | null>(null);
  /** True while the document view holds unsubmitted revision edits; leaving it would drop that text. */
  const [documentDirty, setDocumentDirty] = useState(false);
  const [knowledgeRechecks, setKnowledgeRechecks] = useState<CaseKnowledgeRecheckApi[]>([]);
  useEffect(() => {
    if (saved.status !== 'pending_source_verification') return;
    void getCaseKnowledgeRechecks(saved.id).then(setKnowledgeRechecks).catch(() => setKnowledgeRechecks([]));
  }, [saved.id, saved.status, saved.events.length]);
  useEffect(() => { onDocumentDirtyChange?.(documentDirty); }, [documentDirty, onDocumentDirtyChange]);
  const response = saved.response;
  if (!response) {
    return <DraftCaseView saved={saved} canEdit={canEdit} onEdit={onEdit} onBack={onBack} canManageActions={canManageActions} viewerRole={viewerRole} workflowOperation={workflowOperation} workflowError={workflowError} setWorkflowOperation={setWorkflowOperation} setWorkflowError={setWorkflowError} />;
  }
  const failed = isReviewFailedResponse(response);
  const reviewResult = failed ? null : (response as Extract<ReviewApiResponse, { review_case_id: string }>).review_result;
  const webFindings = failed ? [] : (response as Extract<ReviewApiResponse, { review_case_id: string }>).web_findings ?? [];
  const completedSaved = saved as SavedCaseWithResponse;

  const handleVerdict = (chunkId: string, verdict: CitationVerdict | null) => {
    setCitationVerdict(saved.id, chunkId, verdict);
  };

  /** Every route out of the document view — view switch, page jump, remediation plan — goes through here. */
  const leaveDocumentView = (): boolean => {
    if (detailView !== 'document' || !documentDirty) return true;
    if (!confirmDiscardDocumentEdit()) return false;
    setDocumentDirty(false);
    // The host may unmount this view in the same commit, so it has to hear about it right now.
    onDocumentDirtyChange?.(false);
    return true;
  };
  const switchDetailView = (next: 'document' | 'report' | 'records'): void => {
    if (!leaveDocumentView()) return;
    setDetailView(next);
  };

  return (
    <div className="case-detail">
      <CaseHeader
        saved={completedSaved}
        onBack={() => { if (leaveDocumentView()) onBack(); }}
        onRerun={() => { if (leaveDocumentView()) onRerun(completedSaved.question, completedSaved.materialText); }}
        viewerRole={viewerRole}
      />

      <CaseProgress
        status={saved.status}
        viewerRole={viewerRole}
        remediationPlan={saved.remediationPlan}
        reviewTaskStatus={saved.reviewTask?.status ?? null}
        approvalStarted={Boolean(saved.feishuApproval)}
        onOpenAction={() => { if (leaveDocumentView()) onOpenRemediationPlan?.(); }}
        revealAnswer={focusAgentAnswer}
        onAnswerRevealed={onAgentAnswerFocused}
        actionSlot={<CaseWorkflowActions saved={saved} canManage={canManageActions} allowApplicantAnswer={viewerRole === 'requester'} operation={workflowOperation} error={workflowError} setOperation={setWorkflowOperation} setError={setWorkflowError} onEditMaterial={() => { if (leaveDocumentView()) onEdit(completedSaved); }} compact />}
      />

      {webFindings.some((item) => !item.known_source_id || item.refresh_needed) ? <div className="enterprise-callout enterprise-callout--warning" role="status"><strong>最新官方材料</strong><ul>{webFindings.filter((item) => !item.known_source_id || item.refresh_needed).map((item) => {
        const recheck = knowledgeRechecks.find((entry) => entry.url === item.url);
        const status = recheck?.recheck_status === 'pending'
          ? (recheck.source_status === 'unchanged' ? '官方正文未变化，案件待复核' : '新版已入库，案件待复核')
          : item.known_source_id ? '发现更新迹象，正在核对官方原件'
            : item.excerpt ? '已有搜索摘录，尚待治理核验' : '已发现来源，尚待治理核验';
        return <li key={item.url}><a href={item.url} target="_blank" rel="noreferrer">{item.title}</a><span> · {status}</span></li>;
      })}</ul></div> : null}

      {!failed ? <nav className="case-detail-views" aria-label="案件详情视图">
        <button type="button" className={detailView === 'report' ? 'is-active' : ''} aria-current={detailView === 'report' ? 'page' : undefined} onClick={() => switchDetailView('report')}>审查报告</button>
        <button type="button" className={detailView === 'document' ? 'is-active' : ''} aria-current={detailView === 'document' ? 'page' : undefined} onClick={() => switchDetailView('document')}>原文审阅</button>
        <button type="button" className={detailView === 'records' ? 'is-active' : ''} aria-current={detailView === 'records' ? 'page' : undefined} onClick={() => switchDetailView('records')}>案件资料</button>
      </nav> : null}
      {!failed && detailView === 'document' ? <>
        <DocumentReview
          caseId={completedSaved.id}
          reviewResultId={reviewResult?.review_result_id ?? ''}
          issues={reviewResult?.issues ?? []}
          canManageActions={canManageActions}
          focusTarget={documentTarget}
          onFocusHandled={() => setDocumentTarget(null)}
          onDirtyChange={setDocumentDirty}
          onOpenReportIssue={(issueId) => { if (!leaveDocumentView()) return; setReportIssueTarget(issueId); setDetailView('report'); }}
        />
      </> : null}
      {failed ? (
        <FailedChain response={response} />
      ) : detailView !== 'document' ? (
        <ReviewChain
          saved={completedSaved}
          view={detailView}
          onVerdictChange={handleVerdict}
          viewerRole={viewerRole}
          canManageActions={canManageActions}
          onOpenRemediationPlan={onOpenRemediationPlan}
          onOpenDocument={(selection) => { setDocumentTarget(selection); setDetailView('document'); }}
          focusIssueId={reportIssueTarget}
          onIssueFocused={() => setReportIssueTarget(null)}
        />
      ) : null}
      {failed ? <AuditDisclosure saved={saved} /> : null}
    </div>
  );
}

function DraftCaseView({
  saved,
  canEdit,
  onEdit,
  onBack,
  canManageActions,
  viewerRole,
  workflowOperation,
  workflowError,
  setWorkflowOperation,
  setWorkflowError,
}: {
  saved: SavedCase;
  canEdit: boolean;
  onEdit: (saved: SavedCase) => void;
  onBack: () => void;
  canManageActions: boolean;
  viewerRole: UserRole;
  workflowOperation: string | null;
  workflowError: string | null;
  setWorkflowOperation: (value: string | null) => void;
  setWorkflowError: (value: string | null) => void;
}): JSX.Element {
  const [shareOpen, setShareOpen] = useState(false);
  return (
    <>
    <div className="case-detail">
      <header className="case-header card">
        <div className="case-header__top">
          <button type="button" className="btn-link case-header__back" onClick={onBack}>← 返回工作台</button>
          <div className="case-header__actions">
            <button type="button" className="case-header__action-btn" onClick={() => setShareOpen(true)}>分享案件</button>
          </div>
        </div>
        <div className="case-header__eyebrow">合规审查案件</div>
        <h1 className="case-header__title">{saved.question}</h1>
        <div className="case-header__meta"><span className={'status-chip status-chip--' + saved.status}>{statusLabel(saved.status)}</span><span>{saved.savedAt.replace('T', ' ').slice(0, 16)}</span></div>
      </header>
      <CaseProgress
        status={saved.status}
        viewerRole={viewerRole}
        remediationPlan={saved.remediationPlan}
        actionSlot={<CaseWorkflowActions saved={saved} canManage={canManageActions} allowApplicantAnswer={viewerRole === 'requester'} operation={workflowOperation} error={workflowError} setOperation={setWorkflowOperation} setError={setWorkflowError} onEditMaterial={() => onEdit(saved)} compact />}
      />
      <section className="card draft-case-card">
        <div className="section-title">提交前检查</div>
        <div className="draft-case-card__grid">
          <div><span>业务活动</span><strong>{saved.intake.business_activity || '待补充'}</strong></div>
          <div><span>跨境传输</span><strong>{saved.intake.cross_border_transfer === null ? '待确认' : saved.intake.cross_border_transfer ? '是' : '否'}</strong></div>
          <div><span>境外接收方</span><strong>{saved.intake.overseas_recipient || '待补充'}</strong></div>
          <div><span>材料长度</span><strong>{saved.materialText.length.toLocaleString()} 字符</strong></div>
        </div>
        <p className="draft-case-card__hint">确认材料和关键事实后提交。</p>
        {canEdit && saved.status === 'needs_info' ? <button type="button" className="case-header__action-btn case-header__action-btn--accent" onClick={() => onEdit(saved)}>编辑并补充</button> : null}
      </section>
      <AuditDisclosure saved={saved} includeMaterial />
    </div>
    <ShareCaseDialog caseId={saved.id} isOpen={shareOpen} onClose={() => setShareOpen(false)} />
    </>
  );
}

function EnterpriseDecisionChain({ saved, includeMaterial = true, embedded = false }: { saved: SavedCase; includeMaterial?: boolean; embedded?: boolean }): JSX.Element | null {
  const { materialSnapshot, intakeSnapshot, reviewTask, feishuApproval, signedDecision, report } = saved;
  if (!(includeMaterial && materialSnapshot) && !intakeSnapshot && !reviewTask && !feishuApproval && !signedDecision && !report) return null;

  return (
    <section className={'enterprise-chain' + (embedded ? ' enterprise-chain--embedded' : '')} aria-label="企业决策证据链">
      {includeMaterial && materialSnapshot ? (
        <article className={(embedded ? '' : 'card ') + 'enterprise-record'}>
          <div className="enterprise-record__heading"><div><span>01</span><h2>材料快照</h2></div><code title={materialSnapshot.fingerprint}>{materialSnapshot.fingerprint.slice(0, 12)}</code></div>
          <p>本次审查绑定不可变材料快照，共 {materialSnapshot.version_ids.length} 个原件版本。</p>
          <div className="enterprise-materials">
            {materialSnapshot.version_ids.map((versionId, index) => (
              <div key={versionId}>
                <span><strong>材料版本 {index + 1}</strong><small>已冻结到本次审查</small></span>
                <span><code title={versionId}>{versionId}</code></span>
              </div>
            ))}
          </div>
        </article>
      ) : null}

      {intakeSnapshot ? (
        <article className={(embedded ? '' : 'card ') + 'enterprise-record enterprise-record--rules'}>
          <div className="enterprise-record__heading"><div><span>02</span><h2>申请人确认事实</h2></div><code title={intakeSnapshot.fingerprint}>{intakeSnapshot.fingerprint.slice(0, 12)}</code></div>
          <p>本次审查绑定不可变事实快照，法律路径由 Agent 结合材料和正式法源独立判断。</p>
          <div className="enterprise-record__facts"><div><span>业务活动</span><strong>{intakeSnapshot.intake.business_activity || '未确认'}</strong></div><div><span>人数统计口径</span><strong>{intakeSnapshot.intake.count_period || 'unknown'}</strong></div></div>
        </article>
      ) : null}

      {reviewTask ? (
        <article className={(embedded ? '' : 'card ') + 'enterprise-record'}>
          <div className="enterprise-record__heading"><div><span>03</span><h2>证据化审查任务</h2></div><span className={`task-state task-state--${reviewTask.status}`}>{REVIEW_TASK_STATUS_LABELS[reviewTask.status]}</span></div>
          <div className="enterprise-record__facts">
            <div><span>任务编号</span><code>{reviewTask.id.slice(0, 18)}</code></div>
            <div><span>当前节点</span><strong>{reviewTask.current_node || (reviewTask.status === 'queued' ? '等待 Worker' : '准备审查')}</strong></div>
            <div><span>执行模型</span><strong>{reviewTask.model_id}</strong></div>
            <div><span>执行次数</span><strong>{reviewTask.attempt_count}</strong></div>
          </div>
          {reviewTask.agent_state?.plan?.length ? <div className="enterprise-callout"><strong>Agent 当前计划</strong><ol>{reviewTask.agent_state.plan.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}</ol></div> : null}
          {reviewTask.steps?.length ? <div className="enterprise-materials">{reviewTask.steps.map((step) => <div key={step.number}><span><strong>{step.summary}</strong><small>{step.action}</small></span><span><code>#{step.number}</code></span></div>)}</div> : null}
          {reviewTask.status === 'failed' ? <div className="enterprise-callout enterprise-callout--danger"><strong>{reviewTask.error_category || '审查任务失败'}</strong><span>{reviewTask.error_message || '可由审核人发起重试，失败节点和记录已保留。'}</span></div> : null}
        </article>
      ) : null}

      {(feishuApproval || signedDecision || report) ? (
        <article className={(embedded ? '' : 'card ') + 'enterprise-record enterprise-record--approval'}>
          <div className="enterprise-record__heading"><div><span>04</span><h2>审批与正式归档</h2></div>{signedDecision ? <strong>{statusLabel(saved.status)}</strong> : <span>等待企业决定</span>}</div>
          {feishuApproval ? <div className="approval-ledger"><div><span>飞书审批实例</span><code>{feishuApproval.instance_id}</code></div><div><span>审批状态</span><strong>{approvalStatusLabel(feishuApproval.status)}</strong></div>{feishuApproval.approver_name ? <div><span>审批人</span><strong>{feishuApproval.approver_name}</strong></div> : null}{feishuApproval.decided_at ? <div><span>审批时间</span><strong>{formatTime(feishuApproval.decided_at)}</strong></div> : null}</div> : null}
          {signedDecision ? <div className="signed-decision"><span aria-hidden="true">✓</span><div><strong>最终决定已签署</strong><small>{signedDecision.approver_name || '飞书审批人'} · {signedDecision.decided_at ? formatTime(signedDecision.decided_at) : '审批时间已留痕'}</small></div></div> : null}
        </article>
      ) : null}
    </section>
  );
}

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
}

function CaseWorkflowActions({
  saved,
  canManage,
  allowApplicantAnswer = false,
  operation,
  error,
  setOperation,
  setError,
  onEditMaterial,
  compact = false,
}: CaseWorkflowActionsProps): JSX.Element | null {
  const [agentAnswer, setAgentAnswer] = useState('');
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

  const resumeAgent = (): void => {
    const task = saved.reviewTask;
    const gateId = task?.agent_state?.gate_id;
    if (!task || !gateId || !agentAnswer.trim()) return;
    void execute('answer', async () => {
      const resumed = await answerReviewTask(task.id, gateId, agentAnswer.trim());
      setAgentAnswer('');
      await openCase(saved.id);
      await waitForReviewTask(resumed.id, async () => {
        await openCase(saved.id);
      });
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
      {(canAnswerAgent || canManage && saved.reviewTask?.status === 'waiting_input') ? <div className="enterprise-callout enterprise-callout--warning"><strong>{saved.reviewTask?.agent_state?.pending_question || 'Agent 需要补充信息'}</strong><textarea value={agentAnswer} onChange={(event) => setAgentAnswer(event.target.value)} placeholder="直接回答 Agent 的问题即可" rows={3} /><small>回答会按来源单独记录，不会改写已冻结事实；如需更正事实，请更新事实并重新提交。</small><button type="button" className="case-header__action-btn case-header__action-btn--accent" disabled={operation !== null || !agentAnswer.trim()} onClick={() => resumeAgent()}>{operation === 'answer' ? '正在提交…' : '直接回答'}</button>{onEditMaterial ? <button type="button" className="case-header__action-btn" disabled={operation !== null} onClick={onEditMaterial}>上传或更新材料</button> : null}</div> : null}
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

function approvalStatusLabel(status: NonNullable<SavedCase['feishuApproval']>['status']): string {
  return {
    pending: '审批中',
    approved: '已通过',
    conditionally_approved: '附条件通过',
    rejected: '已退回',
    withdrawn: '已撤回',
  }[status];
}

function pendingFollowupCount(saved: SavedCase): number {
  if (!saved.remediationPlan) return saved.actions.length;
  return saved.remediationPlan.tasks
    .filter((task) => task.is_current !== false && task.status !== 'completed').length;
}

function ReviewFollowupSummary({ saved, onOpen }: { saved: SavedCase; onOpen?: () => void }): JSX.Element | null {
  const currentTasks = (saved.remediationPlan?.tasks ?? []).filter((task) => task.is_current !== false);
  const pending = pendingFollowupCount(saved);
  const pendingReview = currentTasks.filter((task) => task.status === 'pending_review').length;
  const href = `?case=${encodeURIComponent(saved.id)}&remediation=plan`;
  // 报告不重复描述案件阶段：只有当清单里确实还有未完成事项时才给出一行入口。
  if (pending === 0) return null;
  return (
    <div className="review-followup-summary" aria-label="待补充与核实">
      <p className="review-followup-summary__count">
        {pending} 项待核实事项{pendingReview ? ` · ${pendingReview} 项待复核` : ''}
      </p>
      {onOpen ? <button type="button" className="btn-secondary" onClick={onOpen}>查看清单</button> : <a className="btn-secondary" href={href}>查看清单</a>}
    </div>
  );
}

function ReviewRecommendations({ items }: { items: string[] }): JSX.Element | null {
  if (items.length === 0) return null;
  return (
    <div className="review-recommendations">
      <div className="review-recommendations__heading">
        <h2>审查建议</h2>
        <span>{items.length} 项</span>
      </div>
      <ol className="review-recommendations__list">
        {items.map((item, index) => <li key={index}><MarkdownText variant="note">{item}</MarkdownText></li>)}
      </ol>
    </div>
  );
}

function ReviewIssues({
  issues,
  onOpenDocument,
}: {
  issues: ReviewIssue[];
  onOpenDocument: (selection: RevisionSelection) => void;
}): JSX.Element | null {
  if (issues.length === 0) return null;
  return (
    <div className="report-main-issues__list">
      {issues.map((issue) => <article className="report-main-issue" id={`report-issue-${issue.id}`} tabIndex={-1} key={issue.id}>
        <div><span className={'issue-kind issue-kind--' + issue.kind}>{ISSUE_KIND_LABELS[issue.kind] ?? issue.kind}</span><strong>{issue.title}</strong></div>
        {issue.material_evidence.length ? <button type="button" className="btn-link" onClick={() => onOpenDocument({ issue, target: issue.material_evidence[0] })}>定位原文</button> : null}
        <details className="report-main-issue__analysis"><summary>查看分析</summary><MarkdownText variant="note">{issue.finding}</MarkdownText>{issue.unknowns.length ? <ul>{issue.unknowns.map((item, index) => <li key={index}>{item}</li>)}</ul> : null}</details>
      </article>)}
    </div>
  );
}

function Timeline({ events, embedded = false }: { events: SavedCase['events']; embedded?: boolean }): JSX.Element {
  return (
    <section className={(embedded ? '' : 'card ') + 'case-timeline'}>
      <div className="section-title">审计时间线</div>
      <div className="case-timeline__list">
        {events.length === 0 ? <span className="state-block__hint">暂无流程记录。</span> : events.map((event) => (
          <div className="case-timeline__item" key={event.id}><span className="case-timeline__dot" /><div><strong>{eventLabel(event.event_type)}</strong><span>{event.created_at.replace('T', ' ').slice(0, 16)}</span></div></div>
        ))}
      </div>
    </section>
  );
}

function AuditDisclosure({ saved, includeMaterial = false }: { saved: SavedCase; includeMaterial?: boolean }): JSX.Element {
  return (
    <details className="card report-disclosure case-audit-disclosure">
      <summary>案件记录</summary>
      <div className="report-disclosure__body">
        <EnterpriseDecisionChain saved={saved} includeMaterial={includeMaterial} embedded />
        <Timeline events={saved.events} embedded />
      </div>
    </details>
  );
}

function eventLabel(event: string): string {
  const labels: Record<string, string> = {
    case_created: '创建案件',
    case_updated: '更新案件材料',
    status_changed: '变更案件状态',
    review_started: '开始证据化审查',
    review_completed: '生成审查结果',
    review_failed: '审查运行失败',
    knowledge_recheck_pending: '官方法源已核验，案件待复核',
    action_created: '创建整改任务',
    action_updated: '更新整改任务',
    remediation_plan_created: '建立整改计划',
    remediation_plan_activated: '激活整改计划',
    remediation_plan_cancelled: '取消整改计划',
    remediation_task_updated: '调整整改任务',
    remediation_task_started: '开始处理整改任务',
    remediation_submitted: '提交整改结果',
    remediation_submission_reviewed: '复核整改结果',
    feedback_saved: '保存人工反馈',
    feishu_approval_created: '发起飞书审批',
    feishu_decision_written_back: '归档飞书最终决定',
    report_generated: '生成正式报告',
    decision_report_generation_failed: '正式报告生成失败',
  };
  return labels[event] ?? event;
}

function statusLabel(status: CaseStatus): string {
  return CASE_STATUS_LABELS[status];
}

// ---------------------------------------------------------------------------
// CaseHeader — case identity with actions
// ---------------------------------------------------------------------------

interface CaseHeaderProps {
  saved: SavedCaseWithResponse;
  onBack: () => void;
  onRerun: () => void;
  viewerRole: UserRole;
}

function CaseHeader({ saved, onBack, onRerun, viewerRole }: CaseHeaderProps): JSX.Element {
  const [shareOpen, setShareOpen] = useState(false);
  const reportReady = Boolean(saved.report);
  const reportCanGenerate = Boolean(
    saved.report
      || saved.signedDecision
      || (saved.feishuApproval && saved.feishuApproval.status !== 'pending'),
  );
  return (
    <>
    <header className="case-header card">
      <div className="case-header__top">
        <button type="button" className="btn-link case-header__back" onClick={onBack}>
          ← 返回工作台
        </button>
        <div className="case-header__actions">
          {reportCanGenerate ? (
            <a className="case-header__action-btn case-header__action-btn--accent" href={caseReportDownloadUrl(saved.id)} download>
              {reportReady ? '下载完整报告' : '生成完整报告'}
            </a>
          ) : saved.feishuApproval?.status === 'pending' ? (
            <button type="button" className="case-header__action-btn case-header__action-btn--report-pending" disabled>
              审批完成后生成报告
            </button>
          ) : null}
          <button type="button" className="case-header__action-btn case-header__action-btn--share" onClick={() => setShareOpen(true)}>分享案件</button>
          <details className="case-header__more">
            <summary className="case-header__action-btn">更多</summary>
            <div className="case-header__more-menu">
              <button type="button" onClick={onRerun}>以此为模板重审</button>
              <button type="button" onClick={() => downloadMarkdown(saved)}>导出 Markdown</button>
              <button type="button" onClick={() => downloadHtml(saved)}>导出 HTML</button>
            </div>
          </details>
        </div>
      </div>

      <h1 className="case-header__title">{saved.question}</h1>

      <div className="case-header__meta">
        <span className={`status-chip status-chip--${saved.status}`}>{statusLabel(saved.status)}</span>
        <span className="case-header__meta-item">
          <span className="case-header__meta-label">保存于</span>
          <span title={formatTime(saved.savedAt)}>{relativeTime(saved.savedAt)}</span>
        </span>
      </div>
    </header>
    <ShareCaseDialog caseId={saved.id} isOpen={shareOpen} onClose={() => setShareOpen(false)} />
    </>
  );
}

// ---------------------------------------------------------------------------
// FailedChain — compact failure summary
// ---------------------------------------------------------------------------

function FailedChain({ response }: { response: Extract<ReviewApiResponse, { status: 'review_failed' }> }): JSX.Element {
  return (
    <section className="error-box" role="alert">
      <span className="error-box__mark" aria-hidden="true">!</span>
      <div>
        <div style={{ fontWeight: 700, marginBottom: '4px' }}>LLM 审查节点失败</div>
        <div style={{ wordBreak: 'break-word' }}>
          {response.failed_node}：{response.message}
        </div>
        <div style={{ marginTop: '6px', fontSize: '0.8125rem', color: '#64748b' }}>
          已重试 {response.attempts} 次 · 原因：{response.reason}
          {response.trace_id ? ` · Trace ${response.trace_id}` : ''}
        </div>
        <div className="warning-note" style={{ marginTop: '10px' }}>
          案件已保留完整的失败节点与追踪信息，可补充材料后重新运行。
        </div>
      </div>
    </section>
  );
}

// ---------------------------------------------------------------------------
// ReviewChain — the full pipeline timeline + detail sections
// ---------------------------------------------------------------------------

interface ReviewChainProps {
  saved: SavedCaseWithResponse;
  view: 'report' | 'records';
  onVerdictChange: (chunkId: string, verdict: CitationVerdict | null) => void;
  viewerRole: UserRole;
  canManageActions: boolean;
  onOpenRemediationPlan?: () => void;
  onOpenDocument: (selection: RevisionSelection) => void;
  focusIssueId: string | null;
  onIssueFocused: () => void;
}

function ReviewChain({ saved, view, onVerdictChange, viewerRole, canManageActions, onOpenRemediationPlan, onOpenDocument, focusIssueId, onIssueFocused }: ReviewChainProps): JSX.Element {
  const response = saved.response as Extract<ReviewApiResponse, { review_case_id: string }>;
  const result = response.review_result;
  const issues = result.issues ?? [];
  const facts = response.review_facts;
  const selfCheck = response.evidence_self_check;
  const queries = response.retrieval_queries ?? [];
  const agent = response.agent;
  const evidenceChunks = response.evidence_chunks ?? [];
  const verdicts = saved.feedback?.citationVerdicts ?? {};
  const [selectedCitationRef, setSelectedCitationRef] = useState<string | null>(null);
  const [highlightedCitationRef, setHighlightedCitationRef] = useState<string | null>(null);
  const [evidenceDrawerOpen, setEvidenceDrawerOpen] = useState(false);
  const [evidenceAnnouncement, setEvidenceAnnouncement] = useState('');
  const highlightTimer = useRef<number | null>(null);

  const evidenceCount = evidenceChunks.length;
  const citationCount = useMemo(
    () => response.citation_groups.reduce((sum, g) => sum + g.citations.length, 0),
    [response.citation_groups],
  );
  const riskBoundariesForDisplay = useMemo(
    () => Array.from(new Set(result.risk_boundaries.map((item) => item.trim()).filter(Boolean))),
    [result.risk_boundaries],
  );
  const hasRiskBoundaryDisclaimer = useMemo(
    () => riskBoundariesForDisplay.some((item) => DISCLAIMER_SENTENCE_DETECTOR.test(item)),
    [riskBoundariesForDisplay],
  );
  const conclusionForDisplay = useMemo(
    () => cleanConclusionForDisplay(result.conclusion, hasRiskBoundaryDisclaimer),
    [result.conclusion, hasRiskBoundaryDisclaimer],
  );
  const citations = useMemo(
    () => response.citation_groups.flatMap((group) => group.citations),
    [response.citation_groups],
  );
  // 整改清单已经承载了对应建议，报告不再重复展示同一条。
  const outstandingRecommendations = useMemo(() => {
    const covered = new Set(
      (saved.remediationPlan?.tasks ?? [])
        .filter((task) => task.is_current !== false && task.source_recommendation_index !== null)
        .map((task) => task.source_recommendation_index),
    );
    return result.recommended_actions.filter((_, index) => !covered.has(index));
  }, [result.recommended_actions, saved.remediationPlan]);

  useEffect(() => {
    if (!selectedCitationRef && citations[0]?.citation_ref) {
      setSelectedCitationRef(citations[0].citation_ref);
    }
  }, [citations, selectedCitationRef]);

  const handleEvidenceSelect = useCallback((citationRef: string, label: string) => {
    setSelectedCitationRef(citationRef);
    setHighlightedCitationRef(citationRef);
    setEvidenceAnnouncement(`已定位到 ${citationRef}：${label}`);
    if (highlightTimer.current !== null) window.clearTimeout(highlightTimer.current);
    highlightTimer.current = window.setTimeout(() => setHighlightedCitationRef(null), 1500);

    setEvidenceDrawerOpen(true);

    window.requestAnimationFrame(() => {
      const target = document.getElementById(`evidence-${cssId(citationRef)}`);
      if (!target) return;
      target.focus({ preventScroll: true });
    });
  }, []);

  useEffect(() => () => {
    if (highlightTimer.current !== null) window.clearTimeout(highlightTimer.current);
  }, []);

  useEffect(() => {
    if (!focusIssueId || view !== 'report') return;
    const details = document.getElementById('report-issues');
    const target = document.getElementById(`report-issue-${focusIssueId}`);
    if (!details || !target) return;
    if (details instanceof HTMLDetailsElement) details.open = true;
    const analysis = target.querySelector('details');
    if (analysis) analysis.open = true;
    target.scrollIntoView({ behavior: 'smooth', block: 'center' });
    target.focus({ preventScroll: true });
    onIssueFocused();
  }, [focusIssueId, onIssueFocused, view]);

  return (
    <>
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {evidenceAnnouncement}
      </div>
      <div className="review-report-layout">
        <main className="review-report">
          {view === 'report' ? <>
          <section className="case-conclusion report-section" id="report-conclusion">
            <div className="case-conclusion__head">
              <RiskBadge level={result.risk_level} />
            </div>
            {result.legal_path ? <p className="case-conclusion__path">当前适用路径：{result.legal_path}</p> : null}
            <div className="decision-summary" aria-label="审查摘要">
              <span className="decision-summary__label">审查摘要</span>
              <p>{result.decision_summary}</p>
            </div>
             <details className="report-inline-disclosure"><summary>阅读完整审查意见</summary><MarkdownText
              variant="report"
              className="case-conclusion__body"
              onCitationClick={(citationRef) => {
                const citation = citations.find((item) => item.citation_ref === citationRef);
                handleEvidenceSelect(citationRef, citation?.citation_label ?? citationRef);
              }}
            >
              {conclusionForDisplay}
             </MarkdownText></details>
          </section>

          {(pendingFollowupCount(saved) > 0 || outstandingRecommendations.length > 0) ? <section className="report-section review-next-steps" id="report-next">
            <ReviewFollowupSummary saved={saved} onOpen={onOpenRemediationPlan} />
            {outstandingRecommendations.length ? <details className="report-inline-disclosure"><summary>其他建议</summary><ReviewRecommendations items={outstandingRecommendations} /></details> : null}
          </section> : null}

          {issues.length ? <section className="report-main-issues" id="report-issues" aria-label="主要问题">
            <h2>主要问题 <span>{issues.length} 项</span></h2>
            <ReviewIssues
              issues={issues}
              onOpenDocument={onOpenDocument}
            />
          </section> : null}

          <details className="card report-disclosure report-basis" id="report-basis">
            <summary>法律依据 · {citationCount} 条引用</summary>
            <div className="report-disclosure__body">
            <GroundedClaims
              claims={result.claims}
              evidenceChunks={evidenceChunks}
              citations={citations}
              compact
              headerAction={(
                <button
                  type="button"
                  className="evidence-drawer-trigger evidence-drawer-trigger--inline"
                  onClick={() => setEvidenceDrawerOpen(true)}
                  aria-controls="evidence-sidebar"
                  aria-expanded={evidenceDrawerOpen}
                >
                  <span>查看条文</span>
                  <span className="evidence-drawer-trigger__count">{citationCount} 条</span>
                  <span className="evidence-drawer-trigger__icon" aria-hidden="true">↗</span>
                </button>
              )}
              onEvidenceSelect={handleEvidenceSelect}
              onCitationClick={(citationRef) => {
                const citation = citations.find((item) => item.citation_ref === citationRef);
                handleEvidenceSelect(citationRef, citation?.citation_label ?? citationRef);
              }}
            />
            </div>
          </details>

          {riskBoundariesForDisplay.length > 0 ? (
            <details className="card report-disclosure review-boundaries" id="report-boundaries">
              <summary>结论适用边界</summary>
              <div className="report-disclosure__body">
              <div className="warning-list">
                {riskBoundariesForDisplay.map((item, index) => <div className="warning-note" key={index}><MarkdownText variant="note">{item}</MarkdownText></div>)}
              </div>
              </div>
            </details>
          ) : null}

          <details className="card report-disclosure report-review-disclosure" id="report-review">
            <summary>{viewerRole === 'requester' ? '反馈审查问题' : '引用核查与复核意见'}</summary>
            <div className="report-disclosure__body">
              {viewerRole !== 'requester' ? <p className="report-disclosure__intro">核对引用并记录人工复核意见。</p> : null}
              {canManageActions ? <CitationList
                groups={response.citation_groups}
                evidenceChunks={evidenceChunks}
                verdicts={verdicts}
                onVerdictChange={onVerdictChange}
                viewerRole={viewerRole}
              /> : null}
              <FeedbackPanel saved={saved} viewerRole={viewerRole} />
            </div>
          </details>

          </> : null}

          {view === 'records' ? <section className="card report-records-disclosure" id="report-records" aria-label="案件资料">
            <div className="report-disclosure__body">
              <details className="report-record-group">
                <summary>案件材料与版本</summary>
                <div className="case-field">
                  <div className="case-field__label">审查问题</div>
                  <div className="case-field__value">{saved.question}</div>
                </div>
                <div className="case-field">
                  <div className="case-field__label">待审查材料</div>
                  <pre className="case-field__material">{saved.materialText}</pre>
                </div>
                {saved.materialSnapshot ? (
                  <div className="enterprise-materials">
                    {saved.materialSnapshot.version_ids.map((versionId, index) => (
                      <div key={versionId}><span><strong>材料版本 {index + 1}</strong><small>已冻结到本次审查</small></span><span><code title={versionId}>{versionId}</code></span></div>
                    ))}
                  </div>
                ) : null}
              </details>

              {viewerRole === 'admin' ? <details className="report-record-group">
                <summary>技术执行记录</summary>
                <p className="report-record-note">语义证据校验：{response.semantic_grounding?.status === 'supported' ? '有支持依据' : EVIDENCE_STATUS_LABELS[selfCheck.status]}{response.second_retrieval_triggered ? ' · 已触发二次检索' : ''}。这是 Agent 内部证据状态，不代表审核人确认或企业批准。</p>
                {result.trigger_reasons.length > 0 ? (
                  <div className="report-record-subsection">
                    <div className="section-title">触发原因</div>
                    <div className="tag-list">{result.trigger_reasons.map((reason, index) => <span className="tag" key={index}>{reason}</span>)}</div>
                  </div>
                ) : null}
                {agent ? (
                  <AgentExecutionSummary
                    plan={agent.plan}
                    turns={agent.turns}
                    searches={agent.searches}
                    evidenceCount={evidenceCount}
                  />
                ) : null}
                <ProcessDetails
                  facts={facts}
                  queries={queries}
                  agent={agent}
                  selfCheck={selfCheck}
                  evidenceCount={evidenceCount}
                  citationCount={citationCount}
                />
              </details> : null}

              <details className="report-record-group">
                <summary>审批与审计记录</summary>
                <EnterpriseDecisionChain saved={saved} includeMaterial={false} embedded />
                <Timeline events={saved.events} embedded />
              </details>
            </div>
          </section> : null}
        </main>

        {view === 'report' ? <EvidenceSidebar
          groups={response.citation_groups}
          evidenceChunks={evidenceChunks}
          claims={result.claims}
          selectedCitationRef={selectedCitationRef}
          highlightedCitationRef={highlightedCitationRef}
          drawerOpen={evidenceDrawerOpen}
          onCitationSelect={handleEvidenceSelect}
          onCloseDrawer={() => setEvidenceDrawerOpen(false)}
          viewerRole={viewerRole}
        /> : null}
      </div>
    </>
  );
}

function ProcessDetails({
  facts,
  queries,
  agent,
  selfCheck,
  evidenceCount,
  citationCount,
}: {
  facts: ReviewFacts;
  queries: NonNullable<Extract<ReviewApiResponse, { review_case_id: string }>['retrieval_queries']>;
  agent: Extract<ReviewApiResponse, { review_case_id: string }>['agent'];
  selfCheck: Extract<ReviewApiResponse, { review_case_id: string }>['evidence_self_check'];
  evidenceCount: number;
  citationCount: number;
}): JSX.Element {
  return (
    <>
      <section>
        <div className="section-title">材料事实摘要</div>
        <div className="facts-grid">
          {FACT_FIELDS.map((f) => (
            <div className="facts-grid__item" key={f.key}>
              <span className="facts-grid__label">{f.label}</span>
              <span className="facts-grid__value">{f.render(facts)}</span>
            </div>
          ))}
        </div>
      </section>

      <section>
        <div className="section-title">检索查询计划</div>
        {queries.length === 0 ? (
          <div className="state-block__hint">未生成检索查询。</div>
        ) : (
          <div className="query-plan">
            {queries.map((q, i) => (
              <div className="query-plan__item" key={q.query_id}>
                <span className="query-plan__index">{i + 1}</span>
                <span className="query-plan__type">{QUERY_TYPE_LABELS[q.query_type] ?? q.query_type}</span>
                <span className="query-plan__text">{q.text}</span>
              </div>
            ))}
          </div>
        )}
      </section>

      <section>
        <div className="section-title">证据自检</div>
        <div className="selfcheck">
          <div className="selfcheck__row">
            <span className="selfcheck__label">自检状态</span>
            <span className={EVIDENCE_STATUS_BADGE_CLASS[selfCheck.status]}>
              {EVIDENCE_STATUS_LABELS[selfCheck.status]}
            </span>
          </div>
          {agent ? (
            <div className="selfcheck__row">
              <span className="selfcheck__label">自主追加检索</span>
              <span className="selfcheck__value">
                {agent.searches > 1 ? `已进行 ${agent.searches} 轮` : '未触发'}
              </span>
            </div>
          ) : null}
          {evidenceCount > 0 ? (
            <div className="selfcheck__row">
              <span className="selfcheck__label">候选证据</span>
              <span className="selfcheck__value">{evidenceCount} 条 · 已采纳 {citationCount} 条</span>
            </div>
          ) : null}
        </div>

        {selfCheck.triggered_reasons.length > 0 ? (
          <div className="selfcheck__reasons">
            <div className="selfcheck__sublabel">触发原因</div>
            <div className="tag-list">
              {selfCheck.triggered_reasons.map((r, i) => (
                <span className="tag" key={i}>{r}</span>
              ))}
            </div>
          </div>
        ) : null}

        {selfCheck.issues.length > 0 ? (
          <div className="selfcheck__issues">
            <div className="selfcheck__sublabel">检出问题</div>
            <div className="selfcheck__issue-list">
              {selfCheck.issues.map((issue, i) => (
                <div className="selfcheck__issue" key={i}>
                  <span className="selfcheck__issue-type">
                    {EVIDENCE_ISSUE_LABELS[issue.issue_type] ?? issue.issue_type}
                  </span>
                  <span className="selfcheck__issue-desc">{issue.description}</span>
                </div>
              ))}
            </div>
          </div>
        ) : null}
      </section>
    </>
  );
}

function EvidenceSidebar({
  groups,
  evidenceChunks,
  claims,
  selectedCitationRef,
  highlightedCitationRef,
  drawerOpen,
  onCitationSelect,
  onCloseDrawer,
  viewerRole,
}: {
  groups: CitationGroup[];
  evidenceChunks: RetrievalHit[];
  claims: Array<{ supporting_citation_refs: string[] }>;
  selectedCitationRef: string | null;
  highlightedCitationRef: string | null;
  drawerOpen: boolean;
  onCitationSelect: (citationRef: string, label: string) => void;
  onCloseDrawer: () => void;
  viewerRole: UserRole;
}): JSX.Element {
  const chunks = useMemo(() => {
    const map = new Map<string, RetrievalHit>();
    evidenceChunks.forEach((chunk) => map.set(chunk.chunk_id, chunk));
    return map;
  }, [evidenceChunks]);
  const supportingClaims = useMemo(() => {
    const map = new Map<string, number[]>();
    claims.forEach((claim, claimIndex) => {
      (claim.supporting_citation_refs ?? []).forEach((citationRef) => {
        const existing = map.get(citationRef) ?? [];
        map.set(citationRef, [...existing, claimIndex + 1]);
      });
    });
    return map;
  }, [claims]);
  const displayGroups = groups;

  useEffect(() => {
    if (!drawerOpen || !selectedCitationRef) return;
    const frame = window.requestAnimationFrame(() => {
      const target = document.getElementById(`evidence-${cssId(selectedCitationRef)}`);
      if (!target) return;
      target.scrollIntoView({
        behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
        block: 'nearest',
        inline: 'nearest',
      });
      target.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [drawerOpen, selectedCitationRef]);

  return (
    <>
      {drawerOpen ? (
        <button type="button" className="evidence-sidebar__scrim" onClick={onCloseDrawer} aria-label="关闭法源核查" />
      ) : null}
      <aside
        id="evidence-sidebar"
        className={'evidence-sidebar' + (drawerOpen ? ' is-drawer-open' : '')}
        aria-label="引用依据详情"
        aria-hidden={!drawerOpen}
      >
        <div className="evidence-sidebar__head">
          <div>
            <div className="evidence-sidebar__title">法律依据</div>
          </div>
          <div className="evidence-sidebar__head-actions">
            <div className="evidence-sidebar__count">
              {displayGroups.reduce((total, group) => total + group.citations.length, 0)} 条法源
            </div>
            <button type="button" className="evidence-sidebar__close" onClick={onCloseDrawer} aria-label="关闭法律依据">
              关闭
            </button>
          </div>
        </div>
        {displayGroups.length === 0 ? (
          <div className="state-block__hint">暂无可引用依据。</div>
        ) : (
          <div className="evidence-sidebar__list">
            {displayGroups.map((group) => (
              <section className="evidence-sidebar__group" key={group.usage}>
                <div className="evidence-sidebar__group-title">
                  <span>{USAGE_LABELS[group.usage]}</span>
                  <span>{group.citations.length} 条</span>
                </div>
                {group.citations.map((citation) => (
                  <EvidenceCard
                    key={citation.citation_ref || citation.chunk_id}
                    item={{
                      ...citation,
                      groupUsage: group.usage,
                      chunk: chunks.get(citation.chunk_id),
                    }}
                    selected={selectedCitationRef === citation.citation_ref}
                    locating={highlightedCitationRef === citation.citation_ref}
                    supportingClaims={supportingClaims.get(citation.citation_ref) ?? []}
                    onSelect={onCitationSelect}
                    viewerRole={viewerRole}
                  />
                ))}
              </section>
            ))}
          </div>
        )}
      </aside>
    </>
  );
}

function EvidenceCard({
  item,
  selected = false,
  locating = false,
  supportingClaims,
  onSelect,
  viewerRole,
}: {
  item: Citation & { groupUsage: CitationGroup['usage']; chunk?: RetrievalHit };
  selected?: boolean;
  locating?: boolean;
  supportingClaims: number[];
  onSelect: (citationRef: string, label: string) => void;
  viewerRole: UserRole;
}): JSX.Element {
  const label = citationDisplayLabel(item);
  const articleText = item.full_article_text?.trim();
  return (
    <article
      className={
        'evidence-card' +
        (selected ? ' is-open' : '') +
        (locating ? ' is-locating' : '')
      }
      id={`evidence-${cssId(item.citation_ref)}`}
      tabIndex={-1}
      aria-label={label}
    >
      <button
        type="button"
        className="evidence-card__trigger"
        onClick={() => onSelect(item.citation_ref, label)}
        aria-expanded={selected}
        aria-controls={`evidence-detail-${cssId(item.citation_ref)}`}
      >
        <span className="evidence-card__top">
          <span className="evidence-card__index">{label}</span>
        </span>
      </button>
      {selected ? (
        <div className="evidence-card__detail" id={`evidence-detail-${cssId(item.citation_ref)}`}>
          <div className="evidence-card__article-label">{item.title}{item.article_no ? ` · ${item.article_no}` : ''}</div>
          {articleText ? (
            <pre className="evidence-card__full-article">{articleText}</pre>
          ) : (
            <div className="evidence-card__missing-article">当前知识库未收录完整条文</div>
          )}
          {supportingClaims.length > 0 ? (
            <div className="evidence-card__relation">
              支持结论 {supportingClaims.map((claimIndex) => String.fromCharCode(0x245f + claimIndex)).join('、')}
            </div>
          ) : null}
          <div className="evidence-card__meta-grid">
            <div><span>效力状态</span><strong>{LAW_STATUS_LABELS[item.law_status] ?? LAW_STATUS_LABELS.unknown}</strong></div>
            <div><span>适用说明</span><strong>{CITATION_ROLE_LABELS[item.citation_role] ?? '需结合具体事实核对'}</strong></div>
            <div><span>法源类型</span><strong>{DOC_TYPE_LABELS[item.doc_type] ?? item.doc_type}</strong></div>
            <div><span>权威等级</span><strong>{AUTHORITY_LABELS[item.authority] ?? item.authority}</strong></div>
            <div className="evidence-card__meta-wide"><span>发布机关</span><strong>{item.issuing_body || '未提供'}</strong></div>
            <div><span>发布日期</span><strong>{item.publish_date || '未提供'}</strong></div>
            <div><span>生效日期</span><strong>{item.effective_date || '未提供'}</strong></div>
          </div>
          <div className="evidence-card__footer">
            <span>{LAW_STATUS_LABELS[item.law_status] ?? LAW_STATUS_LABELS.unknown}</span>
            {item.source_url ? (
              <a href={item.source_url} target="_blank" rel="noopener noreferrer" onClick={(event) => event.stopPropagation()}>
                打开官方原文 ↗
              </a>
            ) : <span>未提供官方原文链接</span>}
          </div>
          {viewerRole === 'admin' && item.chunk ? (
            <div className="evidence-card__tech">管理员视图 · chunk {shortId(item.chunk.chunk_id)} · {item.chunk.retriever} · {item.chunk.score.toFixed(4)}</div>
          ) : null}
        </div>
      ) : null}
    </article>
  );
}

// ---------------------------------------------------------------------------
// AgentExecutionSummary — actual runtime telemetry, not a fixed workflow
// ---------------------------------------------------------------------------

interface AgentExecutionSummaryProps {
  plan: string[];
  turns: number;
  searches: number;
  evidenceCount: number;
}

function AgentExecutionSummary({
  plan,
  turns,
  searches,
  evidenceCount,
}: AgentExecutionSummaryProps): JSX.Element {
  const steps: Array<{ label: string; detail: string; tone: 'done' | 'warn' | 'neutral' }> = [
    { label: '执行计划', detail: `${plan.length} 项`, tone: plan.length > 0 ? 'done' : 'neutral' },
    { label: '自主执行', detail: `${turns} 次决策`, tone: turns > 0 ? 'done' : 'neutral' },
    { label: '动态检索', detail: `${searches} 轮`, tone: searches > 0 ? 'done' : 'neutral' },
    { label: '证据归集', detail: `${evidenceCount} 条`, tone: evidenceCount > 0 ? 'done' : 'warn' },
    { label: '受控交付', detail: '已完成', tone: 'done' },
  ];

  return (
    <section className="card pipeline">
      <div className="pipeline__track">
        {steps.map((step, i) => (
          <div className={'pipeline__step pipeline__step--' + step.tone} key={i}>
            <div className="pipeline__dot" aria-hidden="true">{i + 1}</div>
            <div className="pipeline__label">{step.label}</div>
            <div className="pipeline__detail">{step.detail}</div>
            {i < steps.length - 1 ? (
              <div className="pipeline__connector" aria-hidden="true" />
            ) : null}
          </div>
        ))}
      </div>
    </section>
  );
}
