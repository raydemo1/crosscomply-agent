/**
 * CaseDetailPage — full review-case workbench view (center column).
 *
 * Renders the complete review chain for a saved case as an auditable
 * timeline, plus product-level affordances the plain workbench lacked:
 *
 *   - sticky case header with id / timestamp / risk + export buttons
 *   - pipeline stepper (事实抽取 → 查询规划 → 混合检索 → 证据自检 → 二次检索 → 结论)
 *   - material & question recap
 *   - facts grid, query plan, evidence self-check (issues + second-retrieval plan)
 *   - conclusion, trigger reasons, recommended actions, risk boundaries
 *   - expandable governed citations (CitationList) with per-citation feedback
 *   - human feedback panel (conclusion usefulness, missing sources, bad case)
 *
 * The page is backed by the server case store; feedback, citation verdicts,
 * actions and workflow transitions remain part of the persisted case record.
 * Failed cases render a compact failure summary instead of the chain.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { CaseStatus, Citation, CitationGroup, RetrievalHit, ReviewApiResponse, ReviewFacts, UserRole } from '../types/api';
import { isReviewFailedResponse } from '../types/api';
import type { CitationVerdict, SavedCase } from '../types/case';
import { setCitationVerdict } from '../store/caseStore';
import { openCase } from '../store/caseStore';
import { caseReportDownloadUrl, createFeishuApproval, retryReviewTask, runCase, waitForReviewTask } from '../api/client';
import RiskBadge from './RiskBadge';
import CitationList from './CitationList';
import FeedbackPanel from './FeedbackPanel';
import GroundedClaims, { cssId } from './GroundedClaims';
import MarkdownText from './MarkdownText';
import { downloadHtml, downloadMarkdown } from '../utils/report';
import { CASE_STATUS_LABELS, REVIEW_TASK_STATUS_LABELS } from '../utils/workflow';
import './RemediationPlanPage.css';
import {
  EVIDENCE_ISSUE_LABELS,
  EVIDENCE_STATUS_BADGE_CLASS,
  EVIDENCE_STATUS_LABELS,
  AUTHORITY_LABELS,
  CITATION_ROLE_LABELS,
  citationDisplayLabel,
  DOC_TYPE_LABELS,
  legalBasisLabel,
  QUERY_TYPE_LABELS,
  USAGE_LABELS,
  formatTime,
  relativeTime,
  renderBool,
  renderList,
  renderText,
  shortId,
} from '../utils/display';

const DEMO_REPORT_DOWNLOAD_URL = '/reports/crosscomply-case-CC-20260818-42AEC816.pdf';

interface CaseDetailPageProps {
  saved: SavedCase;
  demoMode?: boolean;
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
  { key: 'region', label: '地区', render: (f) => renderText(f.region) },
  { key: 'industry', label: '行业', render: (f) => renderText(f.industry) },
  { key: 'missing_information', label: '缺失信息', render: (f) => renderList(f.missing_information) },
];

export default function CaseDetailPage({
  saved,
  demoMode = false,
  canEdit,
  onEdit,
  onRerun,
  onBack,
  canManageActions,
  viewerRole,
  onOpenRemediationPlan,
}: CaseDetailPageProps): JSX.Element {
  const [workflowOperation, setWorkflowOperation] = useState<string | null>(null);
  const [workflowError, setWorkflowError] = useState<string | null>(null);
  const response = saved.response;
  if (!response) {
    return <DraftCaseView saved={saved} canEdit={canEdit} onEdit={onEdit} onBack={onBack} canManageActions={canManageActions} workflowOperation={workflowOperation} workflowError={workflowError} setWorkflowOperation={setWorkflowOperation} setWorkflowError={setWorkflowError} />;
  }
  const failed = isReviewFailedResponse(response);
  const completedSaved = saved as SavedCaseWithResponse;

  const handleVerdict = (chunkId: string, verdict: CitationVerdict | null) => {
    setCitationVerdict(saved.id, chunkId, verdict);
  };

  return (
    <div className="case-detail">
      <CaseHeader
        saved={completedSaved}
        demoMode={demoMode}
        onBack={onBack}
        onRerun={() => onRerun(completedSaved.question, completedSaved.materialText)}
        canManageActions={canManageActions}
        workflowOperation={workflowOperation}
        workflowError={workflowError}
        setWorkflowOperation={setWorkflowOperation}
        setWorkflowError={setWorkflowError}
      />

      <HeroCaseProgress saved={saved} />
      {failed ? (
        <FailedChain response={response} />
      ) : (
        <ReviewChain
          saved={completedSaved}
          demoMode={demoMode}
          onVerdictChange={handleVerdict}
          viewerRole={viewerRole}
          canManageActions={canManageActions}
          onOpenRemediationPlan={onOpenRemediationPlan}
        />
      )}
      {failed ? <RemediationSummary saved={saved} onOpen={onOpenRemediationPlan} /> : null}
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
  workflowOperation: string | null;
  workflowError: string | null;
  setWorkflowOperation: (value: string | null) => void;
  setWorkflowError: (value: string | null) => void;
}): JSX.Element {
  return (
    <div className="case-detail">
      <header className="case-header card">
        <button type="button" className="btn-link case-header__back" onClick={onBack}>← 返回案件管理</button>
        <div className="case-header__eyebrow">案件 {saved.id.slice(0, 18)}</div>
        <h1 className="case-header__title">{saved.question}</h1>
        <div className="case-header__meta"><span className={'status-chip status-chip--' + saved.status}>{statusLabel(saved.status)}</span><span>{saved.savedAt.replace('T', ' ').slice(0, 16)}</span></div>
      </header>
      <HeroCaseProgress saved={saved} />
      <CaseWorkflowActions saved={saved} canManage={canManageActions} operation={workflowOperation} error={workflowError} setOperation={setWorkflowOperation} setError={setWorkflowError} />
      <section className="card draft-case-card">
        <div className="section-title">提交前检查</div>
        <div className="draft-case-card__grid">
          <div><span>业务活动</span><strong>{saved.intake.business_activity || '待补充'}</strong></div>
          <div><span>跨境传输</span><strong>{saved.intake.cross_border_transfer === null ? '待确认' : saved.intake.cross_border_transfer ? '是' : '否'}</strong></div>
          <div><span>境外接收方</span><strong>{saved.intake.overseas_recipient || '待补充'}</strong></div>
          <div><span>材料长度</span><strong>{saved.materialText.length.toLocaleString()} 字符</strong></div>
        </div>
        {saved.ruleDecision?.determination.needs_info.length ? (
          <div className="case-operations__blockers"><strong>送审前必须确认</strong><ul>{saved.ruleDecision.determination.needs_info.map((item) => <li key={item.key}>{item.reason}</li>)}</ul></div>
        ) : null}
        <p className="draft-case-card__hint">确认材料和关键事实后提交。</p>
        {canEdit && saved.status === 'needs_info' ? <button type="button" className="case-header__action-btn case-header__action-btn--accent" onClick={() => onEdit(saved)}>编辑并补充</button> : null}
      </section>
      <AuditDisclosure saved={saved} includeMaterial />
    </div>
  );
}

const HERO_STEPS = [
  ['采购申请', '境外 SaaS 场景'],
  ['材料立卷', '原件与版本哈希'],
  ['事实确认', '关键事实不推测'],
  ['全国路径', '确定性规则判定'],
  ['证据深审', '法源与例外说明'],
  ['补件整改', '缺口闭环'],
  ['飞书审批', '企业最终决定'],
  ['决策归档', '报告与审计留痕'],
] as const;

function currentHeroStep(saved: SavedCase): number {
  return {
    draft: 1,
    needs_info: saved.reviewTask ? 5 : 2,
    pending_review: 3,
    review_running: 4,
    pending_feishu_approval: 6,
    approved: 7,
    conditionally_approved: 7,
    rejected: 7,
    run_failed: 4,
  }[saved.status];
}

function HeroCaseProgress({ saved }: { saved: SavedCase }): JSX.Element {
  const activeStep = currentHeroStep(saved);
  const currentStep = HERO_STEPS[Math.min(activeStep, HERO_STEPS.length - 1)];
  return (
    <details className="card hero-case-progress" aria-label="企业采购境外 SaaS 合规流程">
      <summary className="hero-case-progress__summary">
        <span><small>案件进度</small><strong>{currentStep[0]}</strong></span>
        <span>{Math.min(activeStep + 1, HERO_STEPS.length)} / {HERO_STEPS.length}</span>
      </summary>
      <ol className="hero-case-progress__steps">
        {HERO_STEPS.map(([title, caption], index) => {
          const state = index < activeStep ? 'is-done' : index === activeStep ? 'is-current' : '';
          return (
            <li className={state} key={title} aria-current={index === activeStep ? 'step' : undefined}>
              <span className="hero-case-progress__index">{index < activeStep ? '✓' : String(index + 1).padStart(2, '0')}</span>
              <div><strong>{title}</strong><small>{caption}</small></div>
            </li>
          );
        })}
      </ol>
    </details>
  );
}

function EnterpriseDecisionChain({ saved, includeMaterial = true, embedded = false }: { saved: SavedCase; includeMaterial?: boolean; embedded?: boolean }): JSX.Element | null {
  const { materialSnapshot, ruleDecision, reviewTask, feishuApproval, signedDecision, report } = saved;
  if (!(includeMaterial && materialSnapshot) && !ruleDecision && !reviewTask && !feishuApproval && !signedDecision && !report) return null;

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

      {ruleDecision ? (
        <article className={(embedded ? '' : 'card ') + 'enterprise-record enterprise-record--rules'}>
          <div className="enterprise-record__heading"><div><span>02</span><h2>全国主路径判定</h2></div><code>{ruleDecision.ruleset_version}</code></div>
          <div className="enterprise-paths">
            {ruleDecision.determination.candidate_paths.map((path) => <div key={path.code} className={path.confidence === 'determined' ? 'is-determined' : 'is-possible'}><strong>{path.label}</strong><span>{path.reason}</span></div>)}
          </div>
          {ruleDecision.determination.needs_info.length > 0 ? <div className="enterprise-callout enterprise-callout--warning"><strong>送审前必须确认</strong><ul>{ruleDecision.determination.needs_info.map((fact) => <li key={fact.key}>{fact.reason}</li>)}</ul></div> : null}
          {ruleDecision.determination.requires_rag_human_confirmation ? <div className="enterprise-callout"><strong>需要检索与人工确认</strong><span>{ruleDecision.determination.manual_confirmation_reasons.join('；')}</span></div> : null}
          {ruleDecision.determination.official_bases.length > 0 ? <div className="enterprise-bases">{ruleDecision.determination.official_bases.map((basis) => <a key={basis.basis_id} href={basis.source_url} target="_blank" rel="noreferrer"><strong>{legalBasisLabel(basis.title, basis.article)}</strong><span>{basis.issuing_body} ↗</span></a>)}</div> : null}
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
  operation: string | null;
  error: string | null;
  setOperation: (value: string | null) => void;
  setError: (value: string | null) => void;
  compact?: boolean;
}

function CaseWorkflowActions({
  saved,
  canManage,
  operation,
  error,
  setOperation,
  setError,
  compact = false,
}: CaseWorkflowActionsProps): JSX.Element | null {
  if (!canManage) return null;

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

  const hasAction = saved.status === 'pending_review'
    || saved.status === 'run_failed'
    || saved.status === 'pending_feishu_approval';
  if (!hasAction && !error) return null;

  const controls = (
    <div className="workflow-actions__controls">
      {saved.status === 'pending_review' ? <button type="button" className="case-header__action-btn case-header__action-btn--accent" disabled={operation !== null} onClick={startReview}>{operation === 'run' ? '审查运行中…' : '启动证据化审查'}</button> : null}
      {saved.status === 'run_failed' ? <button type="button" className="case-header__action-btn case-header__action-btn--accent" disabled={operation !== null || !saved.reviewTask} onClick={retryReview}>{operation === 'retry' ? '重新运行中…' : '重试失败任务'}</button> : null}
      {saved.status === 'pending_feishu_approval' && !saved.feishuApproval ? <button type="button" className="case-header__action-btn case-header__action-btn--accent" disabled={operation !== null} onClick={createApproval}>{operation === 'approval' ? '正在创建审批…' : '发起飞书审批'}</button> : null}
    </div>
  );

  if (compact) {
    return <div className="case-header__workflow">{controls}{error ? <div className="case-header__workflow-error" role="alert">{error}</div> : null}</div>;
  }

  return (
    <section className="card workflow-actions" aria-label="审核流程操作">
      <div className="workflow-actions__copy">
        <span>审核人操作</span>
        <strong>{workflowActionTitle(saved)}</strong>
        <small>{workflowActionHint(saved)}</small>
      </div>
      {controls}
      {error ? <div className="workflow-actions__error" role="alert">{error}</div> : null}
    </section>
  );
}

function workflowActionTitle(saved: SavedCase): string {
  if (saved.status === 'pending_review') return '材料与规则已冻结，可以启动审查';
  if (saved.status === 'run_failed') return '失败记录已保留，可以人工重试';
  if (saved.status === 'pending_feishu_approval') return saved.feishuApproval ? '飞书审批已发起，等待权威回写' : '审查已完成，可以发起飞书审批';
  return '流程状态已更新';
}

function workflowActionHint(saved: SavedCase): string {
  if (saved.status === 'pending_review') return '任务将进入 PostgreSQL 队列，由独立 Worker 执行。';
  if (saved.status === 'run_failed') return `失败节点：${saved.reviewTask?.current_node || '未记录'}；重试不会覆盖历史尝试。`;
  if (saved.status === 'pending_feishu_approval') return '最终通过、退回或撤回状态仅接受飞书验签事件。';
  return '流程状态已更新。';
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

function RemediationSummary({ saved, onOpen }: { saved: SavedCase; onOpen?: () => void }): JSX.Element {
  const planTasks = saved.remediationPlan?.tasks ?? [];
  const legacyTasks = saved.remediationPlan ? [] : saved.actions;
  const total = planTasks.length || legacyTasks.length;
  const completed = planTasks.length
    ? planTasks.filter((task) => task.status === 'completed').length
    : legacyTasks.filter((action) => action.status === 'completed').length;
  const pendingReview = planTasks.filter((task) => task.status === 'pending_review').length;
  const today = new Date().toISOString().slice(0, 10);
  const overdue = planTasks.length
    ? planTasks.filter((task) => task.status !== 'completed' && task.due_date && task.due_date < today).length
    : legacyTasks.filter((action) => action.status !== 'completed' && action.due_date && action.due_date < today).length;
  const href = `?case=${encodeURIComponent(saved.id)}&remediation=plan`;
  return (
    <section className="report-section remediation-summary" aria-label="整改计划摘要">
      <div className="remediation-summary__copy">
        <h2>整改计划</h2>
        <p>{saved.remediationPlan ? '独立管理负责人、处理说明和审核验收。' : '尚未建立整改计划，审查建议不会自动变成任务。'}</p>
      </div>
      <div className="remediation-summary__stats"><strong>{completed}/{total}</strong><span>已完成</span><span>{pendingReview} 待复核</span>{overdue ? <span className="is-danger">{overdue} 已逾期</span> : null}</div>
      {onOpen ? <button type="button" className="remediation-button remediation-button--primary" onClick={onOpen}>打开整改计划</button> : <a className="remediation-button remediation-button--primary" href={href}>打开整改计划</a>}
    </section>
  );
}

function ReviewRecommendations({ items }: { items: string[] }): JSX.Element | null {
  if (items.length === 0) return null;
  return (
    <section className="report-section review-recommendations">
      <div className="review-recommendations__heading">
        <h2>审查建议</h2>
        <span>{items.length} 项</span>
      </div>
      <ol className="review-recommendations__list">
        {items.map((item, index) => <li key={index}><MarkdownText variant="note">{item}</MarkdownText></li>)}
      </ol>
    </section>
  );
}

function ReviewGaps({ blockers = [], manualConfirmations = [] }: { blockers?: string[]; manualConfirmations?: string[] }): JSX.Element | null {
  const blockingItems = Array.from(new Set(blockers.filter(Boolean)));
  const confirmationItems = Array.from(new Set(manualConfirmations.filter(Boolean)));
  if (blockingItems.length === 0 && confirmationItems.length === 0) return null;
  return (
    <section className="report-section review-gaps">
      <div className="review-gaps__heading"><div><h2>待补充事实</h2><p>由规则判断和审查结果识别，不是人工创建的整改任务。</p></div></div>
      {blockingItems.length > 0 ? (
        <div className="case-operations__blockers">
          <strong>送审前必须确认</strong>
          <ul>{blockingItems.map((item) => <li key={item}>{item}</li>)}</ul>
        </div>
      ) : null}
      {confirmationItems.length > 0 ? <div className="case-operations__confirmation"><strong>需要人工确认</strong><span>{confirmationItems.join('；')}</span></div> : null}
    </section>
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
      <summary>决策证据链与审计记录</summary>
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
  demoMode: boolean;
  onBack: () => void;
  onRerun: () => void;
  canManageActions: boolean;
  workflowOperation: string | null;
  workflowError: string | null;
  setWorkflowOperation: (value: string | null) => void;
  setWorkflowError: (value: string | null) => void;
}

function CaseHeader({ saved, demoMode, onBack, onRerun, canManageActions, workflowOperation, workflowError, setWorkflowOperation, setWorkflowError }: CaseHeaderProps): JSX.Element {
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
          ← 返回新建案件
        </button>
        <div className="case-header__actions">
          <CaseWorkflowActions saved={saved} canManage={canManageActions} operation={workflowOperation} error={workflowError} setOperation={setWorkflowOperation} setError={setWorkflowError} compact />
          {reportCanGenerate ? (
            demoMode ? (
              <a className="case-header__action-btn case-header__action-btn--accent" href={DEMO_REPORT_DOWNLOAD_URL} download>下载演示 PDF</a>
            ) : (
              <a className="case-header__action-btn case-header__action-btn--accent" href={caseReportDownloadUrl(saved.id)} download>
                {reportReady ? '下载完整报告' : '生成完整报告'}
              </a>
            )
          ) : saved.feishuApproval?.status === 'pending' ? (
            <button type="button" className="case-header__action-btn case-header__action-btn--report-pending" disabled>
              审批完成后生成报告
            </button>
          ) : null}
          <button type="button" className="case-header__action-btn case-header__action-btn--share" onClick={() => setShareOpen(true)}>分享案件</button>
          <details className="case-header__more">
            <summary className="case-header__action-btn">更多</summary>
            <div className="case-header__more-menu">
              {demoMode ? null : <button type="button" onClick={onRerun}>以此为模板重审</button>}
              <button type="button" onClick={() => downloadMarkdown(saved)}>导出 Markdown</button>
              <button type="button" onClick={() => downloadHtml(saved)}>导出 HTML 报告</button>
            </div>
          </details>
        </div>
      </div>

      <h1 className="case-header__title">{saved.question}</h1>

      <div className="case-header__meta">
        <span className={`status-chip status-chip--${saved.status}`}>{statusLabel(saved.status)}</span>
        <span className="case-header__meta-item">
          <span className="case-header__meta-label">案卷</span>
          <code>{shortId(saved.id)}</code>
        </span>
        {!isReviewFailedResponse(saved.response) ? (
          <span className="case-header__meta-item">
            <span className="case-header__meta-label">追踪</span>
            <code>{shortId(saved.response.trace_id)}</code>
          </span>
        ) : null}
        <span className="case-header__meta-item">
          <span className="case-header__meta-label">保存于</span>
          <span title={formatTime(saved.savedAt)}>{relativeTime(saved.savedAt)}</span>
        </span>
        {saved.feedback?.conclusionUseful !== null && saved.feedback?.conclusionUseful !== undefined ? (
          <span className="badge badge-low">
            {saved.feedback.conclusionUseful ? '结论有用' : '结论无用'}
          </span>
        ) : null}
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
  demoMode: boolean;
  onVerdictChange: (chunkId: string, verdict: CitationVerdict | null) => void;
  viewerRole: UserRole;
  canManageActions: boolean;
  onOpenRemediationPlan?: () => void;
}

function ReviewChain({ saved, demoMode, onVerdictChange, viewerRole, canManageActions, onOpenRemediationPlan }: ReviewChainProps): JSX.Element {
  const response = saved.response as Extract<ReviewApiResponse, { review_case_id: string }>;
  const result = response.review_result;
  const facts = response.review_facts;
  const selfCheck = response.evidence_self_check;
  const queries = response.retrieval_queries ?? [];
  const evidenceChunks = response.evidence_chunks ?? [];
  const verdicts = saved.feedback?.citationVerdicts ?? {};
  const [selectedCitationRef, setSelectedCitationRef] = useState<string | null>(null);
  const [highlightedCitationRef, setHighlightedCitationRef] = useState<string | null>(null);
  const [mobileEvidenceOpen, setMobileEvidenceOpen] = useState(false);
  const [evidenceAnnouncement, setEvidenceAnnouncement] = useState('');
  const highlightTimer = useRef<number | null>(null);

  const evidenceCount = evidenceChunks.length;
  const citationCount = useMemo(
    () => response.citation_groups.reduce((sum, g) => sum + g.citations.length, 0),
    [response.citation_groups],
  );

  const citations = useMemo(
    () => response.citation_groups.flatMap((group) => group.citations),
    [response.citation_groups],
  );

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

    const isMobile = window.matchMedia('(max-width: 1100px)').matches;
    if (isMobile) setMobileEvidenceOpen(true);

    window.requestAnimationFrame(() => {
      const target = document.getElementById(`evidence-${cssId(citationRef)}`);
      if (!target) return;
      target.focus({ preventScroll: isMobile });
      if (!isMobile) {
        target.scrollIntoView({
          behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches
            ? 'auto'
            : 'smooth',
          block: 'nearest',
        });
      }
    });
  }, []);

  useEffect(() => () => {
    if (highlightTimer.current !== null) window.clearTimeout(highlightTimer.current);
  }, []);

  return (
    <>
      <div className="sr-only" role="status" aria-live="polite" aria-atomic="true">
        {evidenceAnnouncement}
      </div>
      <div className="review-report-layout">
        <main className="review-report">
          <section className="case-conclusion report-section">
            <div className="case-conclusion__head">
              <RiskBadge level={result.risk_level} />
              <span className="case-conclusion__evidence">
                证据自检：
                <strong>{EVIDENCE_STATUS_LABELS[selfCheck.status]}</strong>
                {response.second_retrieval_triggered ? (
                  <span className="case-conclusion__second">· 已触发二次检索</span>
                ) : null}
              </span>
            </div>
            <div className="decision-summary" aria-label="审批摘要">
              <span className="decision-summary__label">审批摘要</span>
              <p>{result.decision_summary}</p>
            </div>
            <div className="section-title">完整审查意见</div>
            <MarkdownText
              variant="report"
              className="case-conclusion__body"
              onCitationClick={(citationRef) => {
                const citation = citations.find((item) => item.citation_ref === citationRef);
                handleEvidenceSelect(citationRef, citation?.citation_label ?? citationRef);
              }}
            >
              {conclusionForDisplay}
            </MarkdownText>
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
                  <span>法源核查</span>
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
            {riskBoundariesForDisplay.length > 0 ? (
              <div className="case-conclusion__boundaries">
                <div className="section-title">风险边界</div>
                <div className="warning-list">
                  {riskBoundariesForDisplay.map((item, index) => <div className="warning-note" key={index}><MarkdownText variant="note">{item}</MarkdownText></div>)}
                </div>
              </div>
            ) : null}
          </section>

          <ReviewRecommendations items={result.recommended_actions} />

          <ReviewGaps
            blockers={saved.ruleDecision?.determination.needs_info.length
              ? saved.ruleDecision.determination.needs_info.map((item) => item.reason)
              : result.missing_information}
            manualConfirmations={saved.ruleDecision?.determination.manual_confirmation_reasons ?? []}
          />

          <RemediationSummary saved={saved} onOpen={onOpenRemediationPlan} />

          <details className="card report-disclosure">
            <summary>案件输入与材料版本</summary>
            <div className="report-disclosure__body">
              <section>
                <div className="case-field">
                  <div className="case-field__label">审查问题</div>
                  <div className="case-field__value">{saved.question}</div>
                </div>
                <div className="case-field">
                  <div className="case-field__label">待审查材料</div>
                  <pre className="case-field__material">{saved.materialText}</pre>
                </div>
              </section>
              {saved.materialSnapshot ? (
                <section>
                  <div className="section-title">冻结材料版本</div>
                  <div className="enterprise-materials">
                    {saved.materialSnapshot.version_ids.map((versionId, index) => (
                      <div key={versionId}><span><strong>材料版本 {index + 1}</strong><small>已冻结到本次审查</small></span><span><code title={versionId}>{versionId}</code></span></div>
                    ))}
                  </div>
                </section>
              ) : null}
            </div>
          </details>

          <details className="card report-disclosure">
            <summary>查看审查流程</summary>
            <div className="report-disclosure__body">
              {result.trigger_reasons.length > 0 ? (
                <section>
                  <div className="section-title">触发原因</div>
                  <div className="tag-list">{result.trigger_reasons.map((reason, index) => <span className="tag" key={index}>{reason}</span>)}</div>
                </section>
              ) : null}
              <PipelineStepper
                factsCount={facts.data_types.length + (facts.cross_border_transfer ? 1 : 0)}
                queryCount={queries.length}
                evidenceCount={evidenceCount}
                selfCheckStatus={selfCheck.status}
                secondRetrieval={selfCheck.second_retrieval_triggered}
                riskLevel={result.risk_level}
              />
              <ProcessDetails
                facts={facts}
                queries={queries}
                selfCheck={selfCheck}
                evidenceCount={evidenceCount}
                citationCount={citationCount}
              />
            </div>
          </details>

          <details className="card report-disclosure">
            <summary>引用依据复核</summary>
            <div className="report-disclosure__body">
              <CitationList
                groups={response.citation_groups}
                evidenceChunks={evidenceChunks}
                verdicts={verdicts}
                readOnly={demoMode}
                onVerdictChange={onVerdictChange}
                viewerRole={viewerRole}
              />
              {demoMode
                ? <div className="demo-readonly-note">公开演示仅供浏览，人工评价与整改任务需要接入自己的服务端后保存。</div>
                : viewerRole === 'requester' ? <FeedbackPanel saved={saved} /> : null}
            </div>
          </details>

          {viewerRole !== 'requester' && !demoMode ? (
            <section className="report-section reviewer-feedback">
              <div className="section-title">人工复核记录</div>
              <FeedbackPanel saved={saved} />
            </section>
          ) : null}

          <AuditDisclosure saved={saved} />
        </main>

        <EvidenceSidebar
          groups={response.citation_groups}
          evidenceChunks={evidenceChunks}
          claims={result.claims}
          selectedCitationRef={selectedCitationRef}
          highlightedCitationRef={highlightedCitationRef}
          mobileOpen={mobileEvidenceOpen}
          onCitationSelect={handleEvidenceSelect}
          onCloseMobile={() => setMobileEvidenceOpen(false)}
          viewerRole={viewerRole}
        />
      </div>
    </>
  );
}

function ProcessDetails({
  facts,
  queries,
  selfCheck,
  evidenceCount,
  citationCount,
}: {
  facts: ReviewFacts;
  queries: NonNullable<Extract<ReviewApiResponse, { review_case_id: string }>['retrieval_queries']>;
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
          <div className="selfcheck__row">
            <span className="selfcheck__label">二次检索</span>
            <span className="selfcheck__value">
              {selfCheck.second_retrieval_triggered ? '已触发' : '未触发'}
            </span>
          </div>
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
  mobileOpen,
  onCitationSelect,
  onCloseMobile,
  viewerRole,
}: {
  groups: CitationGroup[];
  evidenceChunks: RetrievalHit[];
  claims: Array<{ supporting_citation_refs: string[] }>;
  selectedCitationRef: string | null;
  highlightedCitationRef: string | null;
  mobileOpen: boolean;
  onCitationSelect: (citationRef: string, label: string) => void;
  onCloseMobile: () => void;
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
    if (!mobileOpen || !selectedCitationRef || !window.matchMedia('(max-width: 1100px)').matches) return;
    const frame = window.requestAnimationFrame(() => {
      const target = document.getElementById(`evidence-${cssId(selectedCitationRef)}`);
      if (!target) return;
      target.scrollIntoView({
        behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth',
        block: 'start',
        inline: 'nearest',
      });
      target.focus({ preventScroll: true });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [mobileOpen, selectedCitationRef]);

  return (
    <>
      {mobileOpen ? (
        <button type="button" className="evidence-sidebar__scrim" onClick={onCloseMobile} aria-label="关闭引用详情" />
      ) : null}
      <aside
        className={'evidence-sidebar' + (mobileOpen ? ' is-mobile-open' : '')}
        aria-label="引用依据详情"
        aria-hidden={!mobileOpen && typeof window !== 'undefined' && window.matchMedia('(max-width: 1100px)').matches}
      >
        <div className="evidence-sidebar__head">
          <div>
            <div className="evidence-sidebar__title">法源核查</div>
            <div className="evidence-sidebar__subtitle">完整条文与官方来源</div>
          </div>
          <div className="evidence-sidebar__head-actions">
            <div className="evidence-sidebar__count">
              {displayGroups.reduce((total, group) => total + group.citations.length, 0)} 条法源
            </div>
            <button type="button" className="evidence-sidebar__close" onClick={onCloseMobile} aria-label="关闭引用详情">
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
      >
        <span className="evidence-card__top">
          <span className="evidence-card__index">{label}</span>
        </span>
      </button>
      {selected ? (
        <div className="evidence-card__detail">
          {supportingClaims.length > 0 ? (
            <div className="evidence-card__relation">
              支持结论 {supportingClaims.map((claimIndex) => String.fromCharCode(0x245f + claimIndex)).join('、')}
            </div>
          ) : null}
          <div className="evidence-card__meta-grid">
            <div><span>法源类型</span><strong>{DOC_TYPE_LABELS[item.doc_type] ?? item.doc_type}</strong></div>
            <div><span>权威等级</span><strong>{AUTHORITY_LABELS[item.authority] ?? item.authority}</strong></div>
            <div className="evidence-card__meta-wide"><span>发布机关</span><strong>{item.issuing_body || '未提供'}</strong></div>
            <div><span>发布日期</span><strong>{item.publish_date || '未提供'}</strong></div>
            <div><span>生效日期</span><strong>{item.effective_date || '未提供'}</strong></div>
          </div>
          <div className="evidence-card__article-label">完整条文</div>
          {articleText ? (
            <pre className="evidence-card__full-article">{articleText}</pre>
          ) : (
            <div className="evidence-card__missing-article">当前知识库未收录完整条文</div>
          )}
          <div className="evidence-card__footer">
            <span>{CITATION_ROLE_LABELS[item.citation_role] ?? '引用角色未提供'}</span>
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
// PipelineStepper — visual pipeline timeline
// ---------------------------------------------------------------------------

interface PipelineStepperProps {
  factsCount: number;
  queryCount: number;
  evidenceCount: number;
  selfCheckStatus: string;
  secondRetrieval: boolean;
  riskLevel: string;
}

function PipelineStepper({
  factsCount,
  queryCount,
  evidenceCount,
  selfCheckStatus,
  secondRetrieval,
  riskLevel,
}: PipelineStepperProps): JSX.Element {
  const steps: Array<{ label: string; detail: string; tone: 'done' | 'warn' | 'neutral' }> = [
    { label: '事实抽取', detail: `${factsCount} 项`, tone: 'done' },
    { label: '查询规划', detail: `${queryCount} 条查询`, tone: queryCount > 0 ? 'done' : 'neutral' },
    { label: '混合检索', detail: `${evidenceCount} 条证据`, tone: evidenceCount > 0 ? 'done' : 'neutral' },
    {
      label: '证据自检',
      detail: selfCheckStatus === 'sufficient' ? '证据充分' : selfCheckStatus === 'insufficient' ? '证据不足' : '需二次检索',
      tone: selfCheckStatus === 'sufficient' ? 'done' : selfCheckStatus === 'insufficient' ? 'warn' : 'warn',
    },
    { label: '二次检索', detail: secondRetrieval ? '已触发' : '未触发', tone: secondRetrieval ? 'warn' : 'neutral' },
    { label: '结论生成', detail: riskLabel(riskLevel), tone: riskLevel === 'high' ? 'warn' : 'done' },
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

function riskLabel(level: string): string {
  if (level === 'high') return '高风险';
  if (level === 'medium') return '中风险';
  if (level === 'low') return '低风险';
  return '证据不足';
}
