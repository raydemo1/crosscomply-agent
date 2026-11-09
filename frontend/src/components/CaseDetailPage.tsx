import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { ArrowRight, ChevronDown, Download, FolderOpen, History, ShieldCheck, Terminal } from 'lucide-react';
import type { CaseKnowledgeRecheckApi, CaseStatus, ReviewApiResponse, ReviewFacts, ReviewIssue, UserRole } from '../types/api';
import { isReviewFailedResponse } from '../types/api';
import type { CitationVerdict, SavedCase } from '../types/case';
import { setCitationVerdict } from '../store/caseStore';
import { openCase } from '../store/caseStore';
import { caseReportDownloadUrl, getCaseKnowledgeRechecks } from '../api/client';
import RiskBadge from './RiskBadge';
import CaseWorkflowActions from './CaseWorkflowActions';
import CaseFactsPanel from './CaseFactsPanel';
import CaseAssistant, { CaseAssistantTrigger } from './CaseAssistant';
import ReviewWorklog from './ReviewWorklog';
import { caseFactLedger, factValue } from '../utils/factFields';
import { cssId } from '../utils/display';
import CitationList from './CitationList';
import FeedbackPanel from './FeedbackPanel';

import MarkdownText from './MarkdownText';
import ReportConclusion, { ReportAnalysis } from './ReportConclusion';
import ReportHeading from './ReportHeading';
import { partitionReportContent } from '../utils/reportContent';
import { PUBLIC_DEMO } from '../runtime';
import ShareCaseDialog from './ShareCaseDialog';
import type { RevisionSelection } from './RevisionWorkspace';
import DocumentReview from './DocumentReview';
import CaseProgress from './CaseProgress';
import { CASE_STATUS_LABELS, REVIEW_TASK_STATUS_LABELS, confirmDiscardDocumentEdit } from '../utils/workflow';
import '../styles/case-detail.css';
import './RemediationPlanPage.css';
import {
  EVIDENCE_ISSUE_LABELS,
  EVIDENCE_STATUS_BADGE_CLASS,
  EVIDENCE_STATUS_LABELS,
  ISSUE_KIND_LABELS,
  citationDisplayLabel,
  QUERY_TYPE_LABELS,
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
  /** 审查任务落定（成功或失败）时由工作底稿触发，宿主立即刷新案件数据。 */
  onReviewSettled?: () => void;
  /** Set by the host when arriving here must land on the Agent question rather than the report top. */
  focusAgentAnswer?: boolean;
  onAgentAnswerFocused?: () => void;
  /** Reports unsubmitted revision or annotation text so the navigation host can guard its own routes out. */
  onDocumentDirtyChange?: (dirty: boolean) => void;
}

export default function CaseDetailPage(props: CaseDetailPageProps): JSX.Element {
  const task = props.saved.reviewTask;
  const key = `${props.saved.id}/${task?.id}/${task?.material_snapshot_id}/${task?.intake_snapshot_id}`;
  return <CaseAssistant key={key} saved={props.saved} viewerRole={props.viewerRole} canManageActions={props.canManageActions}><CaseDetailContent {...props} /></CaseAssistant>;
}

type SavedCaseWithResponse = SavedCase & { response: ReviewApiResponse };
const NO_REVIEW_ISSUES: ReviewIssue[] = [];

function focusAnswer(): void {
  const target = document.getElementById('case-agent-answer');
  target?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  target?.focus({ preventScroll: true });
}

/** Ordered facts shown in the 材料事实摘要 grid. */
const FACT_FIELDS: Array<{ key: string; label: string; render: (f: ReviewFacts) => string }> = [
  { key: 'business_activity', label: '业务活动', render: (f) => renderText(f.business_activity) },
  { key: 'cross_border_transfer', label: '跨境传输', render: (f) => renderBool(f.cross_border_transfer) },
  { key: 'overseas_recipient', label: '境外接收方', render: (f) => renderText(f.overseas_recipient) },
  { key: 'data_types', label: '数据类型', render: (f) => renderList(f.data_types) },
  { key: 'sensitive_personal_info', label: '敏感个人信息', render: (f) => renderBool(f.sensitive_personal_info) },
  { key: 'processing_purpose', label: '处理目的', render: (f) => renderText(f.processing_purpose) },
  { key: 'legal_basis', label: '告知、同意及其他处理依据', render: (f) => renderText(f.legal_basis_or_consent) },
  { key: 'regions', label: '地区', render: (f) => renderList(f.regions) },
  { key: 'industry', label: '行业', render: (f) => renderText(f.industry) },
  { key: 'as_of_date', label: '法规适用日期', render: (f) => f.as_of_date || '审查当天' },
  { key: 'missing_information', label: '缺失信息', render: (f) => renderList(f.missing_information) },
];

function CaseDetailContent({
  saved,
  canEdit,
  onEdit,
  onRerun,
  onBack,
  canManageActions,
  viewerRole,
  onOpenRemediationPlan,
  onReviewSettled,
  focusAgentAnswer,
  onAgentAnswerFocused,
  onDocumentDirtyChange,
}: CaseDetailPageProps): JSX.Element {
  const [workflowOperation, setWorkflowOperation] = useState<string | null>(null);
  const [workflowError, setWorkflowError] = useState<string | null>(null);
  const [detailView, setDetailView] = useState<'document' | 'report' | 'facts' | 'records'>('report');
  const [documentTarget, setDocumentTarget] = useState<RevisionSelection | null>(null);
  const [reportIssueTarget, setReportIssueTarget] = useState<string | null>(null);
  const [recordTarget, setRecordTarget] = useState<string | null>(null);
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
    return <DraftCaseView saved={saved} canEdit={canEdit} onEdit={onEdit} onBack={onBack} canManageActions={canManageActions} viewerRole={viewerRole} workflowOperation={workflowOperation} workflowError={workflowError} setWorkflowOperation={setWorkflowOperation} setWorkflowError={setWorkflowError} focusAgentAnswer={focusAgentAnswer} onAgentAnswerFocused={onAgentAnswerFocused} onReviewSettled={onReviewSettled} />;
  }
  const failed = isReviewFailedResponse(response);
  const reviewResult = failed ? null : (response as Extract<ReviewApiResponse, { review_case_id: string }>).review_result;
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
  const switchDetailView = (next: 'document' | 'report' | 'facts' | 'records'): void => {
    if (!leaveDocumentView()) return;
    setDetailView(next);
  };
  const attentionCount = new Set(caseFactLedger(saved).filter((entry) => entry.status === 'conflicted' || entry.source_type === 'applicant_statement').map((entry) => entry.field)).size;
  const waiting = saved.reviewTask?.status === 'waiting_input';
  const openRecord = (id: string): void => {
    if (!leaveDocumentView()) return;
    setRecordTarget(id);
    setDetailView('records');
  };
  const progress = <CaseProgress
    embedded={!failed}
    caseId={saved.id}
    canRereview={canEdit}
    beforeRereview={leaveDocumentView}
    onRereviewSettled={onReviewSettled}
    status={saved.status}
    viewerRole={viewerRole}
    remediationPlan={saved.remediationPlan}
    reviewTaskStatus={saved.reviewTask?.status ?? null}
    approvalStarted={Boolean(saved.feishuApproval)}
    onAnswerAction={waiting ? () => { if (leaveDocumentView()) focusAnswer(); } : undefined}
    onOpenAction={() => { if (leaveDocumentView()) onOpenRemediationPlan?.(); }}
    actionSlot={!waiting ? <CaseWorkflowActions saved={saved} canManage={canManageActions} allowApplicantAnswer={viewerRole === 'requester'} operation={workflowOperation} error={workflowError} setOperation={setWorkflowOperation} setError={setWorkflowError} onEditMaterial={() => { if (leaveDocumentView()) onEdit(completedSaved); }} compact /> : undefined}
  />;

  return (
    <div className="case-detail case-detail--reviewed">
      <CaseHeader
        saved={completedSaved}
        onBack={() => { if (leaveDocumentView()) onBack(); }}
        onRerun={() => { if (leaveDocumentView()) onRerun(completedSaved.question, completedSaved.materialText); }}
        onOpenProgress={failed ? undefined : () => openRecord('record-progress')}
      />
      {failed ? progress : null}
      <div className={'case-workspace' + (detailView === 'document' ? ' case-workspace--document' : '')}>
        <div className="case-workspace__main">
          {!failed ? <nav className="case-detail-views" aria-label="案件详情视图">
            <button type="button" className={detailView === 'report' ? 'is-active' : ''} aria-current={detailView === 'report' ? 'page' : undefined} onClick={() => switchDetailView('report')}>审查报告</button>
            <button type="button" className={detailView === 'facts' ? 'is-active' : ''} aria-current={detailView === 'facts' ? 'page' : undefined} onClick={() => switchDetailView('facts')}>案件事实{attentionCount > 0 ? <span className="case-facts-count" aria-label={`${attentionCount} 项待核对`}>{attentionCount}</span> : null}</button>
            <button type="button" className={detailView === 'document' ? 'is-active' : ''} aria-current={detailView === 'document' ? 'page' : undefined} onClick={() => switchDetailView('document')}>原文审阅</button>
            <button type="button" className={detailView === 'records' ? 'is-active' : ''} aria-current={detailView === 'records' ? 'page' : undefined} onClick={() => switchDetailView('records')}>案件资料</button>
          </nav> : null}
          {waiting ? <CaseWorkflowActions saved={saved} canManage={canManageActions} allowApplicantAnswer={viewerRole === 'requester'} operation={workflowOperation} error={workflowError} setOperation={setWorkflowOperation} setError={setWorkflowError} onEditMaterial={() => { if (leaveDocumentView()) onEdit(completedSaved); }} focusAnswer={focusAgentAnswer} onAnswerFocused={onAgentAnswerFocused} /> : null}
          {detailView === 'facts' || failed ? <CaseFactsPanel saved={saved} viewerRole={viewerRole} onOpenDocument={failed ? undefined : () => switchDetailView('document')} /> : null}
          {!failed && detailView === 'document' ?
            <DocumentReview
              caseId={completedSaved.id}
              reviewResultId={reviewResult?.review_result_id ?? ''}
              issues={reviewResult?.issues ?? NO_REVIEW_ISSUES}
              fallbackMaterialText={saved.materialText}
              canManageActions={canManageActions}
              focusTarget={documentTarget}
              onFocusHandled={() => setDocumentTarget(null)}
              onDirtyChange={setDocumentDirty}
              onOpenReportIssue={(issueId) => { if (!leaveDocumentView()) return; setReportIssueTarget(issueId); setDetailView('report'); }}
            />
          : null}
          {failed ? (
            <FailedChain response={response} />
          ) : detailView === 'report' || detailView === 'records' ? (
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
              progress={progress}
              knowledgeRechecks={knowledgeRechecks}
              recordTarget={recordTarget}
              onRecordFocused={() => setRecordTarget(null)}
              onOpenRecord={openRecord}
              onViewDocument={() => switchDetailView('document')}
            />
          ) : null}
          {failed ? <AuditDisclosure saved={saved} /> : null}
        </div>
      </div>
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
  focusAgentAnswer,
  onAgentAnswerFocused,
  onReviewSettled,
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
  focusAgentAnswer?: boolean;
  onAgentAnswerFocused?: () => void;
  onReviewSettled?: () => void;
}): JSX.Element {
  const [shareOpen, setShareOpen] = useState(false);
  return (
    <>
    <div className="case-detail">
      <header className="case-header">
        <div className="case-header__top">
          <button type="button" className="btn-link case-header__back" onClick={onBack}>← 返回案件列表</button>
          <div className="case-header__actions"><CaseAssistantTrigger />
            <button type="button" className="case-header__action-btn" onClick={() => setShareOpen(true)}>分享案件</button>
          </div>
        </div>
        <h1 className="case-header__title">{saved.question}</h1>
        <div className="case-header__meta"><span className={'status-chip status-chip--' + saved.status}>{statusLabel(saved.status)}</span><time dateTime={saved.savedAt} title={new Date(saved.savedAt).toLocaleString('zh-CN')}>{relativeTime(saved.savedAt)}</time></div>
      </header>
      <CaseProgress
        status={saved.status}
        viewerRole={viewerRole}
        remediationPlan={saved.remediationPlan}
        reviewTaskStatus={saved.reviewTask?.status ?? null}
        approvalStarted={Boolean(saved.feishuApproval)}
        onAnswerAction={saved.reviewTask?.status === 'waiting_input' ? focusAnswer : undefined}
        actionSlot={saved.reviewTask?.status !== 'waiting_input' ? <CaseWorkflowActions saved={saved} canManage={canManageActions} allowApplicantAnswer={viewerRole === 'requester'} operation={workflowOperation} error={workflowError} setOperation={setWorkflowOperation} setError={setWorkflowError} onEditMaterial={() => onEdit(saved)} compact /> : undefined}
      />
      {saved.reviewTask && (saved.reviewTask.status === 'queued' || saved.reviewTask.status === 'running') ? <ReviewWorklog taskId={saved.reviewTask.id} onSettled={() => onReviewSettled?.()} /> : (
      <div className="case-workspace">
        <div className="case-workspace__main">
          {saved.reviewTask?.status === 'waiting_input' ? <CaseWorkflowActions saved={saved} canManage={canManageActions} allowApplicantAnswer={viewerRole === 'requester'} operation={workflowOperation} error={workflowError} setOperation={setWorkflowOperation} setError={setWorkflowError} onEditMaterial={() => onEdit(saved)} focusAnswer={focusAgentAnswer} onAnswerFocused={onAgentAnswerFocused} /> : null}
          {!saved.reviewTask ? <section className="card draft-case-card">
            <div className="section-title">提交前检查</div>
            <div className="draft-case-card__grid">
              <div><span>业务活动</span><strong>{saved.intake.business_activity || '待补充'}</strong></div>
              <div><span>跨境传输</span><strong>{saved.intake.cross_border_transfer === null ? '待确认' : saved.intake.cross_border_transfer ? '是' : '否'}</strong></div>
              <div><span>境外接收方</span><strong>{saved.intake.overseas_recipient || '待补充'}</strong></div>
              <div><span>材料长度</span><strong>{saved.materialText.length.toLocaleString()} 字符</strong></div>
            </div>
            <p className="draft-case-card__hint">确认材料和关键事实后提交。</p>
            {canEdit && saved.status === 'needs_info' ? <button type="button" className="case-header__action-btn case-header__action-btn--accent" onClick={() => onEdit(saved)}>编辑并补充</button> : null}
          </section> : saved.reviewTask.status !== 'waiting_input' ? <section className="card case-review-placeholder" role="status"><span className="case-module-kicker">审查报告</span><h2>{saved.reviewTask.status === 'queued' ? '材料已提交，等待审查' : saved.reviewTask.status === 'running' ? '正在审查案件' : '本次审查尚未形成报告'}</h2><p>完成后，判断结论与需要处理的问题会显示在这里。</p></section> : null}
          <CaseFactsPanel saved={saved} viewerRole={viewerRole} />
          <AuditDisclosure saved={saved} includeMaterial />
        </div>
      </div>
      )}
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
          <div className="enterprise-record__heading"><div><span>01</span><h2>本次送审材料</h2></div></div>
          <p>已锁定送审版本，共 {materialSnapshot.version_ids.length} 份原件。</p>
          <div className="enterprise-materials">
            {materialSnapshot.version_ids.map((versionId, index) => (
              <div key={versionId}>
                <span><strong>材料版本 {index + 1}</strong><small>已锁定送审版本</small></span>

              </div>
            ))}
          </div>
        </article>
      ) : null}

      {intakeSnapshot ? (
        <article className={(embedded ? '' : 'card ') + 'enterprise-record enterprise-record--rules'}>
          <div className="enterprise-record__heading"><div><span>02</span><h2>申报人已确认事实</h2></div></div>

          <div className="enterprise-record__facts"><div><span>业务活动</span><strong>{intakeSnapshot.intake.business_activity || '未确认'}</strong></div><div><span>人数统计口径</span><strong>{factValue('count_period', intakeSnapshot.intake.count_period)}</strong></div></div>
        </article>
      ) : null}

      {reviewTask ? (
        <article className={(embedded ? '' : 'card ') + 'enterprise-record'}>
          <div className="enterprise-record__heading"><div><span>03</span><h2>CrossComply 证据审查</h2></div><span className={`task-state task-state--${reviewTask.status}`}>{REVIEW_TASK_STATUS_LABELS[reviewTask.status]}</span></div>
          {reviewTask.current_node ? <div className="enterprise-record__facts"><div><span>当前节点</span><strong>{reviewTask.current_node}</strong></div></div> : null}
          {reviewTask.steps?.length ? <div className="enterprise-materials">{reviewTask.steps.map((step) => <div key={step.number}><span><strong>{step.summary}</strong><small>{step.action}</small></span></div>)}</div> : null}
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
  if (!saved.remediationPlan) return 0;
  return saved.remediationPlan.tasks
    .filter((task) => task.is_current !== false && task.status !== 'completed').length;
}

function ReviewFollowupSummary({ saved, onOpen }: { saved: SavedCase; onOpen?: () => void }): JSX.Element | null {
  const currentTasks = (saved.remediationPlan?.tasks ?? []).filter((task) => task.is_current !== false);
  const pending = pendingFollowupCount(saved);
  const pendingReview = currentTasks.filter((task) => task.status === 'pending_review').length;
  const href = `?case=${encodeURIComponent(saved.id)}&remediation=plan`;
  const [showAll, setShowAll] = useState(false);
  if (pending === 0) return null;
  const PREVIEW = 3;
  const openTasks = currentTasks.filter((task) => task.status !== 'completed');
  const preview = openTasks.slice(0, PREVIEW);
  const rest = openTasks.slice(PREVIEW);
  return (
    <div className="review-followup-summary" aria-label="待补充与核实">
      <ReportHeading title="待核实事项" count={`${pending} 项${pendingReview ? ` · ${pendingReview} 项待复核` : ''}`} action={onOpen ? <button type="button" className="btn-link" onClick={onOpen}>查看完整清单 →</button> : <a className="btn-link" href={href}>查看完整清单 →</a>} />
      <ul className="review-followup-summary__items">
        {(showAll ? openTasks : preview).map((task) => <li key={task.id}><span className={`review-item-state review-item-state--${task.status}`}>{task.status === 'pending_review' ? '待复核' : task.status === 'in_progress' ? '处理中' : '待处理'}</span><span className="review-followup-summary__item-title">{task.title}</span>{task.due_date ? <small>截止 {task.due_date}</small> : null}</li>)}
      </ul>
      {rest.length ? <button type="button" className="review-followup-summary__more" aria-expanded={showAll} onClick={() => setShowAll((value) => !value)}><span className="rest-toggle">{showAll ? '收起' : `展开其余 ${rest.length} 项`}<ChevronDown size={15} strokeWidth={1.8} aria-hidden="true" /></span></button> : null}
    </div>
  );
}

function ReviewRecommendations({ items }: { items: string[] }): JSX.Element | null {
  if (items.length === 0) return null;
  return <section className="review-recommendations" aria-label="下一步行动">
    <ReportHeading title="下一步行动" count={`${items.length} 项`} />
    <ol className="review-recommendations__list">{items.map((item, index) => <li key={index}>
      <span className="review-recommendations__index" aria-hidden="true">{index + 1}</span>
      <MarkdownText variant="note" allowRawHtml={false}>{item}</MarkdownText>
    </li>)}</ol>
  </section>;
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
    <ol className="report-main-issues__list" role="list">
      {issues.map((issue, index) => <li className="report-main-issue" id={`report-issue-${issue.id}`} tabIndex={-1} key={issue.id}>
        <span className="report-main-issue__index" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>
        <div className="report-main-issue__heading"><h3>{issue.title}</h3><span className={'issue-kind issue-kind--' + issue.kind}>{ISSUE_KIND_LABELS[issue.kind] ?? issue.kind}</span></div>
        <details className="report-main-issue__analysis"><summary><ChevronDown size={14} aria-hidden="true" />查看分析</summary><MarkdownText variant="note" allowRawHtml={false}>{issue.finding}</MarkdownText>{issue.unknowns.length ? <ul>{issue.unknowns.map((item, index) => <li key={index}>{item}</li>)}</ul> : null}</details>
        {issue.material_evidence.length ? <button type="button" className="btn-link" onClick={() => onOpenDocument({ issue, target: issue.material_evidence[0] })}>定位原文</button> : null}
      </li>)}
    </ol>
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
      <summary><History size={16} strokeWidth={1.8} aria-hidden="true" />案件记录</summary>
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
    knowledge_recheck_pending: '法规来源已核验，案件待复核',
    action_created: '创建整改任务',
    action_updated: '更新整改任务',
    remediation_plan_created: '建立待办事项清单',
    remediation_plan_activated: '激活待办事项清单',
    remediation_plan_cancelled: '取消待办事项清单',
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
  onOpenProgress?: () => void;
}

function CaseHeader({ saved, onBack, onRerun, onOpenProgress }: CaseHeaderProps): JSX.Element {
  const [shareOpen, setShareOpen] = useState(false);
  const actionsRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const close = (event: MouseEvent): void => {
      if (!(event.target instanceof Node)) return;
      for (const details of actionsRef.current?.querySelectorAll<HTMLDetailsElement>('.case-header__dropdown[open]') ?? []) {
        if (!details.contains(event.target)) details.open = false;
      }
    };
    document.addEventListener('click', close);
    return () => document.removeEventListener('click', close);
  }, []);

  const closeDropdown = (trigger: HTMLElement): void => {
    const details = trigger.closest('details');
    if (!details?.open) return;
    details.open = false;
    details.querySelector('summary')?.focus({ preventScroll: true });
  };
  const exportCase = async (format: 'markdown' | 'html'): Promise<void> => {
    const { downloadMarkdown, downloadHtml } = await import('../utils/report');
    (format === 'markdown' ? downloadMarkdown : downloadHtml)(saved);
  };
  const reportReady = Boolean(saved.report);
  const reportCanGenerate = Boolean(
    saved.report
      || saved.signedDecision
      || (saved.feishuApproval && saved.feishuApproval.status !== 'pending'),
  );
  return (
    <>
    <header className="case-header case-header--reviewed">
      <div className="case-header__top">
        <button type="button" className="btn-link case-header__back" onClick={onBack}>
          ← 返回案件列表
        </button>
        <div className="case-header__actions" ref={actionsRef} onKeyDown={(event) => {
          if (event.key !== 'Escape' || !(event.target instanceof HTMLElement) || !event.target.closest('.case-header__dropdown[open]')) return;
          event.stopPropagation();
          closeDropdown(event.target);
        }}>
          <CaseAssistantTrigger />
          <details className="case-header__dropdown">
            <summary className="case-header__action-btn"><Download size={16} aria-hidden="true" />导出案件<ChevronDown size={14} aria-hidden="true" /></summary>
            <div className="case-header__dropdown-menu">
              {reportCanGenerate ? <a href={caseReportDownloadUrl(saved.id)} download onClick={(event) => closeDropdown(event.currentTarget)}>{reportReady ? '下载完整报告' : '生成完整报告'}</a> : null}
              <button type="button" onClick={(event) => { closeDropdown(event.currentTarget); void exportCase('markdown'); }}>导出 Markdown</button>
              <button type="button" onClick={(event) => { closeDropdown(event.currentTarget); void exportCase('html'); }}>导出 HTML</button>
            </div>
          </details>
          {!PUBLIC_DEMO ? <details className="case-header__dropdown">
            <summary className="case-header__action-btn">更多<ChevronDown size={14} aria-hidden="true" /></summary>
            <div className="case-header__dropdown-menu">
              <button type="button" onClick={(event) => { closeDropdown(event.currentTarget); setShareOpen(true); }}>分享案件</button>
              <button type="button" onClick={(event) => { closeDropdown(event.currentTarget); onRerun(); }}>以此为模板重审</button>
            </div>
          </details> : null}
        </div>
      </div>

      <h1 className="case-header__title">{saved.question}</h1>

      <div className="case-header__meta">
        <span className={`status-chip status-chip--${saved.status}`}>{statusLabel(saved.status)}</span>
        {onOpenProgress ? <button type="button" className="btn-link case-view-link case-header__progress-link" onClick={onOpenProgress}>查看流程 <ArrowRight size={14} aria-hidden="true" /></button> : null}
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
        <div className="failed-chain__title">本次审查未完成</div>
        <div className="failed-chain__detail">
          本次审查未完成，请重试。
        </div>
        <div className="failed-chain__meta">
          已自动重试 {response.attempts} 次 · 失败环节：{response.reason}
        </div>
        <div className="warning-note failed-chain__note">
          案件与材料均已保留，技术细节可在下方「技术执行记录」中查看。
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
  progress: ReactNode;
  knowledgeRechecks: CaseKnowledgeRecheckApi[];
  recordTarget: string | null;
  onRecordFocused: () => void;
  onOpenRecord: (id: string) => void;
  onViewDocument: () => void;
}

function ReviewChain({ saved, view, onVerdictChange, viewerRole, canManageActions, onOpenRemediationPlan, onOpenDocument, focusIssueId, onIssueFocused, progress, knowledgeRechecks, recordTarget, onRecordFocused, onOpenRecord, onViewDocument }: ReviewChainProps): JSX.Element {
  const response = saved.response as Extract<ReviewApiResponse, { review_case_id: string }>;
  const result = response.review_result;
  const issues = result.issues ?? NO_REVIEW_ISSUES;
  const { body, conditions, usageNotes } = useMemo(
    () => partitionReportContent(result.conclusion, result.risk_boundaries),
    [result.conclusion, result.risk_boundaries],
  );
  const facts = response.review_facts;
  const selfCheck = response.evidence_self_check;
  const queries = response.retrieval_queries ?? [];
  const sourceLeads = (response.web_findings ?? []).filter((item) => !item.known_source_id || item.refresh_needed);
  const agent = response.agent;
  const evidenceChunks = response.evidence_chunks ?? [];
  const verdicts = saved.feedback?.citationVerdicts ?? {};
  const evidenceCount = evidenceChunks.length;
  const citations = useMemo(
    () => response.citation_groups.flatMap((group) => group.citations),
    [response.citation_groups],
  );
  const citationCount = citations.length;
  // 整改清单已经承载了对应建议，报告不再重复展示同一条。
  const outstandingRecommendations = useMemo(() => {
    const covered = new Set(
      (saved.remediationPlan?.tasks ?? [])
        .filter((task) => task.is_current !== false && task.source_recommendation_index !== null)
        .map((task) => task.source_recommendation_index),
    );
    return result.recommended_actions.filter((_, index) => !covered.has(index));
  }, [result.recommended_actions, saved.remediationPlan]);
  const hasFollowup = pendingFollowupCount(saved) > 0;
  const hasNextSteps = hasFollowup || outstandingRecommendations.length > 0;

  const openCitation = (citationRef: string): void => {
    const clause = document.getElementById(`report-clause-${cssId(citationRef)}`);
    if (clause instanceof HTMLDetailsElement) clause.open = true;
    const target = clause?.querySelector('summary') ?? document.getElementById('report-basis');
    target?.focus({ preventScroll: true });
    target?.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' });
  };

  useEffect(() => {
    if (!recordTarget || view !== 'records') return;
    const record = document.getElementById(recordTarget);
    if (record instanceof HTMLDetailsElement) {
      record.open = true;
      record.querySelector('summary')?.focus({ preventScroll: true });
      record.scrollIntoView({ block: 'nearest' });
    }
    onRecordFocused();
  }, [recordTarget, onRecordFocused, view]);

  useEffect(() => {
    if (!focusIssueId || view !== 'report') return;
    const target = document.getElementById(`report-issue-${focusIssueId}`);
    if (!target) return;
    const analysis = target.querySelector('details');
    if (analysis) analysis.open = true;
    target.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'center' });
    target.focus({ preventScroll: true });
    onIssueFocused();
  }, [focusIssueId, onIssueFocused, view]);

  return (
      <div className="review-report-layout">
        <article className="review-report review-report--document" aria-label={view === 'report' ? '审查报告' : undefined} data-matter-source={view === 'report' ? 'report' : undefined} data-report-id={result.review_result_id}>
          {view === 'report' ? <>
          <ReportConclusion result={result} conditions={conditions} />

          {issues.length ? <section className="report-main-issues" id="report-issues" aria-label="主要问题">
            <ReportHeading title="主要问题" count={`${issues.length} 项`} />
            <ReviewIssues
              issues={issues}
              onOpenDocument={onOpenDocument}
            />
          </section> : null}

          {hasNextSteps ? <section className="report-section review-next-steps" id="report-next">
            {hasFollowup ? <ReviewFollowupSummary saved={saved} onOpen={onOpenRemediationPlan} /> : null}
            {outstandingRecommendations.length ? <ReviewRecommendations items={outstandingRecommendations} /> : null}
          </section> : null}

          <ReportAnalysis body={body} onCitationClick={openCitation} />

          <section className="report-section report-basis" id="report-basis" tabIndex={-1}>
            <ReportHeading title="法规依据" count={citationCount ? `${citationCount} 条` : undefined} />
              {result.legal_basis_gaps?.length ? <div className="report-record-subsection">
                <h3>待核实的法规依据</h3>
                <ul>{result.legal_basis_gaps.map((gap, index) => <li key={index}>{gap}</li>)}</ul>
              </div> : null}
              {citations.length ? <ul className="basis-clause-list" role="list">
                {citations.map((citation) => {
                  const label = citationDisplayLabel(citation);
                  const article = citation.article_no?.trim() ?? '';
                  const articleIndex = article ? label.lastIndexOf(article) : -1;
                  return <li key={citation.citation_ref}>
                    <details className="basis-clause" id={`report-clause-${cssId(citation.citation_ref)}`}>
                      <summary>
                        <span className="basis-clause__name">{articleIndex > 0 ? <>
                          {label.slice(0, articleIndex)}<span className="basis-clause__article">{article}</span>{label.slice(articleIndex + article.length)}
                        </> : label}</span>
                        <ChevronDown className="basis-clause__chevron" size={16} aria-hidden="true" />
                      </summary>
                      <div className="basis-clause__body">
                        {citation.full_article_text?.trim() ? <blockquote className="basis-clause__text">{citation.full_article_text}</blockquote> : <p className="basis-clause__text basis-clause__text--missing">暂无条文原文。</p>}
                      </div>
                    </details>
                  </li>;
                })}
              </ul> : <p className="report-disclosure__intro">暂无可引用依据。</p>}
              {sourceLeads.length ? <button type="button" className="btn-link case-view-link report-basis__leads" onClick={() => onOpenRecord('record-source-leads')}>待核验的法规线索 <ArrowRight size={14} aria-hidden="true" /></button> : null}
          </section>
          {usageNotes.length ? <footer className="report-footnote">本报告不构成正式法律意见。</footer> : null}
          </> : null}

          {view === 'records' ? <section className="case-records" id="report-records" aria-labelledby="case-records-title">
            <header className="case-panel-heading">
              <h2 id="case-records-title">案件资料</h2>
              <span>最近保存 <time dateTime={saved.savedAt}>{formatTime(saved.savedAt)}</time></span>
            </header>
            <div className="case-records__list">
              <details className="report-record-group" id="record-progress">
                <summary><History size={17} aria-hidden="true" /><span>审查流程</span><ChevronDown className="case-disclosure-chevron" size={16} aria-hidden="true" /></summary>
                {progress}
              </details>

              <details className="report-record-group">
                <summary><FolderOpen size={17} aria-hidden="true" /><span>案件材料与版本</span><ChevronDown className="case-disclosure-chevron" size={16} aria-hidden="true" /></summary>
                <div className="case-records__material">
                  {saved.materialSnapshot?.version_ids.length ? (
                  <div className="enterprise-materials">
                    {saved.materialSnapshot.version_ids.map((versionId, index) => (
                      <div key={versionId}><span><strong>材料版本 {index + 1}</strong><small>已锁定送审版本</small></span><span><code title={versionId}>{versionId}</code></span></div>
                    ))}
                  </div>
                  ) : <p className="report-record-note">暂无已锁定的送审版本。</p>}
                  <button type="button" className="btn-link case-view-link" onClick={onViewDocument}>查看送审原文 <ArrowRight size={14} aria-hidden="true" /></button>
                </div>
              </details>

              {!PUBLIC_DEMO || citations.length ? <details className="report-record-group">
                <summary><ShieldCheck size={17} aria-hidden="true" /><span>{PUBLIC_DEMO ? '法规来源' : canManageActions ? '引用核查与复核意见' : '法规来源与审查反馈'}</span><ChevronDown className="case-disclosure-chevron" size={16} aria-hidden="true" /></summary>
                {citations.length ? <CitationList groups={response.citation_groups} evidenceChunks={evidenceChunks} claims={result.claims} verdicts={verdicts} onVerdictChange={onVerdictChange} viewerRole={viewerRole} readOnly={!canManageActions} /> : null}
                {!PUBLIC_DEMO ? <FeedbackPanel saved={saved} viewerRole={viewerRole} /> : null}
              </details> : null}

              {sourceLeads.length ? <details className="report-record-group" id="record-source-leads">
                <summary><FolderOpen size={17} aria-hidden="true" /><span>待核验的法规线索</span><small>{sourceLeads.length} 条</small><ChevronDown className="case-disclosure-chevron" size={16} aria-hidden="true" /></summary>
                <ul className="case-records__sources">{sourceLeads.map((item) => {
                  const recheck = knowledgeRechecks.find((entry) => entry.url === item.url);
                  const status = !item.is_official ? '第三方资料，仅供参考' : recheck?.recheck_status === 'pending'
                    ? (recheck.source_status === 'unchanged' ? '官方正文未变化，案件待复核' : '新版已入库，案件待复核')
                    : item.known_source_id ? '发现更新线索，尚待核对原件'
                      : item.excerpt ? '网页摘录待核验' : '已发现来源，待核验';
                  return <li key={item.url}><a href={item.url} target="_blank" rel="noreferrer">{item.title}</a><small>{status}</small></li>;
                })}</ul>
              </details> : null}

              <details className="report-record-group">
                <summary><ShieldCheck size={17} aria-hidden="true" /><span>审批与操作记录</span><ChevronDown className="case-disclosure-chevron" size={16} aria-hidden="true" /></summary>
                <EnterpriseDecisionChain saved={saved} includeMaterial={false} embedded />
                <Timeline events={saved.events} embedded />
              </details>

              {viewerRole === 'admin' ? <details className="report-record-group">
                <summary><Terminal size={17} aria-hidden="true" /><span>技术执行记录</span><ChevronDown className="case-disclosure-chevron" size={16} aria-hidden="true" /></summary>
                <p className="report-record-note">结论依据核验：{response.semantic_grounding?.status === 'supported' ? '有支持依据' : EVIDENCE_STATUS_LABELS[selfCheck.status]}{response.second_retrieval_triggered ? ' · 已触发二次检索' : ''}。</p>
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

            </div>
          </section> : null}
        </article>

        {view === 'report' ? <nav className="report-toc" aria-label="报告章节">{[
          ['report-conclusion', '审查结论'],
          ...(issues.length ? [['report-issues', '主要问题']] : []),
          ...(hasNextSteps ? [['report-next', hasFollowup && !outstandingRecommendations.length ? '待核实事项' : '下一步行动']] : []),
          ...(body ? [['report-analysis', '完整分析']] : []),
          ['report-basis', '法规依据'],
        ].map(([id, title]) => <a href={`#${id}`} key={id} onClick={(event) => { event.preventDefault(); const target = document.getElementById(id); if (target instanceof HTMLDetailsElement) target.open = true; target?.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'start' }); }}>{title}</a>)}</nav> : null}
      </div>
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
    { label: '合规输出', detail: '已完成', tone: 'done' },
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
