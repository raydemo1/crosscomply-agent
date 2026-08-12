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
import type { CaseAction, CaseStatus, Citation, CitationGroup, RetrievalHit, ReviewApiResponse, ReviewFacts, UserRole } from '../types/api';
import { isReviewFailedResponse } from '../types/api';
import type { CitationVerdict, SavedCase } from '../types/case';
import { createCaseAction, setActionStatus, setCitationVerdict, updateCaseAction } from '../store/caseStore';
import RiskBadge from './RiskBadge';
import CitationList from './CitationList';
import FeedbackPanel from './FeedbackPanel';
import GroundedClaims, { cssId } from './GroundedClaims';
import MarkdownText from './MarkdownText';
import { downloadHtml, downloadMarkdown } from '../utils/report';
import {
  EVIDENCE_ISSUE_LABELS,
  EVIDENCE_STATUS_BADGE_CLASS,
  EVIDENCE_STATUS_LABELS,
  AUTHORITY_LABELS,
  CITATION_ROLE_LABELS,
  DOC_TYPE_LABELS,
  LAW_STATUS_LABELS,
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
  /** Persist a workflow status transition. */
  onStatusChange: (caseId: string, status: CaseStatus) => void;
  /** Reviewers and admins can maintain persisted remediation actions. */
  canManageActions: boolean;
  viewerRole: UserRole;
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
  canEdit,
  onEdit,
  onRerun,
  onBack,
  onStatusChange,
  canManageActions,
  viewerRole,
}: CaseDetailPageProps): JSX.Element {
  const response = saved.response;
  if (!response) {
    return <DraftCaseView saved={saved} canEdit={canEdit} onEdit={onEdit} onBack={onBack} onStatusChange={onStatusChange} />;
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
        onBack={onBack}
        onRerun={() => onRerun(completedSaved.question, completedSaved.materialText)}
      />

      <CaseOperations saved={saved} onStatusChange={onStatusChange} canManageActions={canManageActions} />

      {failed ? (
        <FailedChain response={response} />
      ) : (
        <ReviewChain
          saved={completedSaved}
          onVerdictChange={handleVerdict}
          viewerRole={viewerRole}
        />
      )}
    </div>
  );
}

function DraftCaseView({
  saved,
  canEdit,
  onEdit,
  onBack,
  onStatusChange,
}: {
  saved: SavedCase;
  canEdit: boolean;
  onEdit: (saved: SavedCase) => void;
  onBack: () => void;
  onStatusChange: (caseId: string, status: CaseStatus) => void;
}): JSX.Element {
  return (
    <div className="case-detail">
      <header className="case-header card">
        <button type="button" className="btn-link case-header__back" onClick={onBack}>← 返回案件工作台</button>
        <div className="case-header__eyebrow">案件 {saved.id.slice(0, 18)}</div>
        <h1 className="case-header__title">{saved.question}</h1>
        <div className="case-header__meta"><span className={'status-chip status-chip--' + saved.status}>{statusLabel(saved.status)}</span><span>{saved.savedAt.replace('T', ' ').slice(0, 16)}</span></div>
      </header>
      <section className="card draft-case-card">
        <div className="section-title">提交前检查</div>
        <div className="draft-case-card__grid">
          <div><span>业务活动</span><strong>{saved.intake.business_activity || '待补充'}</strong></div>
          <div><span>跨境传输</span><strong>{saved.intake.cross_border_transfer === null ? '待确认' : saved.intake.cross_border_transfer ? '是' : '否'}</strong></div>
          <div><span>境外接收方</span><strong>{saved.intake.overseas_recipient || '待补充'}</strong></div>
          <div><span>材料长度</span><strong>{saved.materialText.length.toLocaleString()} 字符</strong></div>
        </div>
        <p className="draft-case-card__hint">请确认案件材料和关键事实后提交。提交后由合规审核人运行证据化审查。</p>
        {canEdit && saved.status === 'needs_info' ? <button type="button" className="case-header__action-btn case-header__action-btn--accent" onClick={() => onEdit(saved)}>编辑并补充</button> : null}
        {!canEdit && saved.status === 'draft' ? <button type="button" className="case-header__action-btn case-header__action-btn--accent" onClick={() => onStatusChange(saved.id, 'submitted')}>提交审核</button> : null}
      </section>
      <Timeline events={saved.events} />
    </div>
  );
}

type ActionFormState = {
  title: string;
  description: string;
  owner_role: string;
  priority: CaseAction['priority'];
  due_date: string;
};

const EMPTY_ACTION_FORM: ActionFormState = {
  title: '',
  description: '',
  owner_role: 'reviewer',
  priority: 'medium',
  due_date: '',
};

function CaseOperations({ saved, onStatusChange, canManageActions }: { saved: SavedCase; onStatusChange: (caseId: string, status: CaseStatus) => void; canManageActions: boolean }): JSX.Element {
  const openActions = saved.actions.filter((action) => action.status !== 'completed').length;
  const [actionFormOpen, setActionFormOpen] = useState(false);
  const [editingActionId, setEditingActionId] = useState<string | null>(null);
  const [actionForm, setActionForm] = useState<ActionFormState>(EMPTY_ACTION_FORM);

  const beginCreateAction = (): void => {
    setEditingActionId(null);
    setActionForm({ ...EMPTY_ACTION_FORM });
    setActionFormOpen(true);
  };

  const beginEditAction = (action: CaseAction): void => {
    setEditingActionId(action.id);
    setActionForm({
      title: action.title,
      description: action.description,
      owner_role: action.owner_role,
      priority: action.priority,
      due_date: action.due_date ?? '',
    });
    setActionFormOpen(true);
  };

  const saveAction = (): void => {
    if (!actionForm.title.trim()) return;
    const payload = {
      title: actionForm.title.trim(),
      description: actionForm.description.trim(),
      owner_role: actionForm.owner_role.trim() || 'reviewer',
      priority: actionForm.priority,
      status: editingActionId ? saved.actions.find((action) => action.id === editingActionId)?.status ?? 'open' : 'open',
      due_date: actionForm.due_date || null,
    };
    if (editingActionId) updateCaseAction(editingActionId, payload);
    else createCaseAction(saved.id, payload);
    setActionFormOpen(false);
    setEditingActionId(null);
  };

  return (
    <section className="card case-operations">
      <div className="case-operations__heading"><div><div className="report-kicker">案件流程</div><h2>从证据到行动</h2></div><span className={`status-chip status-chip--${saved.status}`}>{statusLabel(saved.status)}</span></div>
      <div className="case-operations__stats"><span><strong>{saved.actions.length}</strong> 项整改动作</span><span><strong>{openActions}</strong> 项待处理</span><span><strong>{saved.events.length}</strong> 条审计记录</span></div>
      {saved.actions.length > 0 ? (
        <div className="case-operations__list">
          {saved.actions.map((action) => (
            <div className="case-action-row" key={action.id}>
              <div className="case-action-row__content"><strong>{action.title}</strong><span>{action.description || '暂无说明'}</span><small>负责人：{action.owner_role} · 优先级：{action.priority}{action.due_date ? ` · 截止：${action.due_date}` : ''}</small></div>
              <div className="case-action-row__controls">
                {canManageActions ? <select aria-label={`动作状态：${action.title}`} className={`action-status action-status--${action.status}`} value={action.status} onChange={(event) => setActionStatus(action.id, event.target.value as CaseAction['status'])}><option value="open">待处理</option><option value="in_progress">进行中</option><option value="completed">已完成</option></select> : <span className={`action-status action-status--${action.status}`}>{action.status === 'completed' ? '已完成' : action.status === 'in_progress' ? '进行中' : '待处理'}</span>}
                {canManageActions ? <button type="button" className="case-action-edit" onClick={() => beginEditAction(action)}>编辑</button> : null}
              </div>
            </div>
          ))}
        </div>
      ) : null}
      {canManageActions && actionFormOpen ? (
        <div className="case-action-form">
          <div className="section-title">{editingActionId ? '编辑整改动作' : '新增整改动作'}</div>
          <div className="case-action-form__grid">
            <label className="form-field"><span>动作名称</span><input value={actionForm.title} onChange={(event) => setActionForm({ ...actionForm, title: event.target.value })} placeholder="例如：完成个人信息保护影响评估" /></label>
            <label className="form-field"><span>负责人</span><input value={actionForm.owner_role} onChange={(event) => setActionForm({ ...actionForm, owner_role: event.target.value })} placeholder="例如：法务、隐私或业务负责人" /></label>
            <label className="form-field"><span>优先级</span><select value={actionForm.priority} onChange={(event) => setActionForm({ ...actionForm, priority: event.target.value as ActionFormState['priority'] })}><option value="high">高</option><option value="medium">中</option><option value="low">低</option></select></label>
            <label className="form-field"><span>截止日期</span><input type="date" value={actionForm.due_date} onChange={(event) => setActionForm({ ...actionForm, due_date: event.target.value })} /></label>
            <label className="form-field form-field--wide"><span>动作说明</span><textarea value={actionForm.description} onChange={(event) => setActionForm({ ...actionForm, description: event.target.value })} placeholder="说明完成标准、交付物或风险背景" /></label>
          </div>
          <div className="case-action-form__actions"><button type="button" className="case-header__action-btn" onClick={() => setActionFormOpen(false)}>取消</button><button type="button" className="case-header__action-btn case-header__action-btn--accent" disabled={!actionForm.title.trim()} onClick={saveAction}>保存动作</button></div>
        </div>
      ) : null}
      <div className="case-operations__actions">
        {canManageActions ? <button type="button" className="case-header__action-btn" onClick={beginCreateAction}>+ 新增整改动作</button> : null}
        {saved.status === 'needs_info' ? <button type="button" className="case-header__action-btn" onClick={() => onStatusChange(saved.id, 'submitted')}>重新提交补充材料</button> : null}
        {saved.status === 'completed' ? <span className="case-operations__complete">✓ 审核已完成</span> : null}
      </div>
    </section>
  );
}

function Timeline({ events }: { events: SavedCase['events'] }): JSX.Element {
  return (
    <section className="card case-timeline">
      <div className="section-title">审计时间线</div>
      <div className="case-timeline__list">
        {events.length === 0 ? <span className="state-block__hint">暂无流程记录。</span> : events.map((event) => (
          <div className="case-timeline__item" key={event.id}><span className="case-timeline__dot" /><div><strong>{eventLabel(event.event_type)}</strong><span>{event.created_at.replace('T', ' ').slice(0, 16)}</span></div></div>
        ))}
      </div>
    </section>
  );
}

function eventLabel(event: string): string {
  const labels: Record<string, string> = { case_created: '创建案件', case_updated: '更新案件材料', status_changed: '变更案件状态', review_started: '开始证据化审查', review_completed: '生成审查结果', review_failed: '审查运行失败', action_created: '生成整改动作', action_updated: '更新整改动作', feedback_saved: '保存人工反馈' };
  return labels[event] ?? event;
}

function statusLabel(status: CaseStatus): string {
  return { draft: '草稿', submitted: '待审核', in_review: '审查中', needs_info: '待补充', completed: '已完成', review_failed: '运行失败' }[status];
}

// ---------------------------------------------------------------------------
// CaseHeader — sticky header with identity + actions
// ---------------------------------------------------------------------------

interface CaseHeaderProps {
  saved: SavedCaseWithResponse;
  onBack: () => void;
  onRerun: () => void;
}

function CaseHeader({ saved, onBack, onRerun }: CaseHeaderProps): JSX.Element {
  const response = saved.response;
  const failed = isReviewFailedResponse(response);
  const risk = failed ? null : response.review_result.risk_level;

  return (
    <header className="case-header card">
      <div className="case-header__top">
        <button type="button" className="btn-link case-header__back" onClick={onBack}>
          ← 返回工作台
        </button>
        <div className="case-header__actions">
          <button type="button" className="case-header__action-btn" onClick={onRerun}>
            以此为模板重审
          </button>
          <button type="button" className="case-header__action-btn" onClick={() => downloadMarkdown(saved)}>
            导出 Markdown
          </button>
          <button type="button" className="case-header__action-btn case-header__action-btn--accent" onClick={() => downloadHtml(saved)}>
            导出 HTML 报告
          </button>
        </div>
      </div>

      <h1 className="case-header__title">{saved.question}</h1>

      <div className="case-header__meta">
        {risk ? (
          <span className="case-header__risk">
            <RiskBadge level={risk} />
          </span>
        ) : (
          <span className="badge badge-insufficient">审查失败</span>
        )}
        <span className="case-header__meta-item">
          <span className="case-header__meta-label">案卷</span>
          <code>{shortId(saved.id)}</code>
        </span>
        {!failed ? (
          <span className="case-header__meta-item">
            <span className="case-header__meta-label">追踪</span>
            <code>{shortId(response.trace_id)}</code>
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
  onVerdictChange: (chunkId: string, verdict: CitationVerdict | null) => void;
  viewerRole: UserRole;
}

function ReviewChain({ saved, onVerdictChange, viewerRole }: ReviewChainProps): JSX.Element {
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
          <section className="card case-conclusion report-card">
            <div className="report-kicker">审查报告</div>
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
            <div className="section-title">审查结论</div>
            <MarkdownText
              variant="report"
              className="case-conclusion__body"
              onCitationClick={(citationRef) => {
                const citation = citations.find((item) => item.citation_ref === citationRef);
                handleEvidenceSelect(citationRef, citation?.citation_label ?? citationRef);
              }}
            >
              {result.conclusion}
            </MarkdownText>
            <GroundedClaims
              claims={result.claims}
              evidenceChunks={evidenceChunks}
              citations={citations}
              compact
              onEvidenceSelect={handleEvidenceSelect}
              onCitationClick={(citationRef) => {
                const citation = citations.find((item) => item.citation_ref === citationRef);
                handleEvidenceSelect(citationRef, citation?.citation_label ?? citationRef);
              }}
            />
          </section>

          <section className="card report-card">
            <div className="section-title">审查问题与材料</div>
            <div className="case-field">
              <div className="case-field__label">审查问题</div>
              <div className="case-field__value">{saved.question}</div>
            </div>
            <div className="case-field">
              <div className="case-field__label">
                待审查材料
                {saved.materialSource ? (
                  <span className="case-field__source">（来源：{saved.materialSource}）</span>
                ) : null}
              </div>
              <pre className="case-field__material">{saved.materialText}</pre>
            </div>
          </section>

          <ReportSection title="建议动作" items={result.recommended_actions} ordered />
          <ReportSection title="风险边界" items={result.risk_boundaries} tone="warning" />
          <ReportSection title="缺失信息" items={result.missing_information} tone="missing" />

          {result.trigger_reasons.length > 0 ? (
            <section className="card report-card">
              <div className="section-title">触发原因</div>
              <div className="tag-list">
                {result.trigger_reasons.map((reason, i) => (
                  <span className="tag" key={i}>{reason}</span>
                ))}
              </div>
            </section>
          ) : null}

          <details className="card report-disclosure">
            <summary>展开审查流程与调试信息</summary>
            <div className="report-disclosure__body">
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
            <summary>展开引用治理与人工反馈</summary>
            <div className="report-disclosure__body">
              <CitationList
                groups={response.citation_groups}
                evidenceChunks={evidenceChunks}
                verdicts={verdicts}
                onVerdictChange={onVerdictChange}
                viewerRole={viewerRole}
              />
              <FeedbackPanel saved={saved} />
            </div>
          </details>
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

function ReportSection({
  title,
  items,
  ordered = false,
  tone,
}: {
  title: string;
  items: string[];
  ordered?: boolean;
  tone?: 'warning' | 'missing';
}): JSX.Element | null {
  if (items.length === 0) return null;
  const className = tone === 'warning' ? 'warning-list' : tone === 'missing' ? 'missing-list' : 'action-list';
  return (
    <section className="card report-card">
      <div className="section-title">{title}</div>
      {ordered ? (
        <ol className={className}>
          {items.map((item, i) => (
            <li className="action-list__item" key={i}>
              <MarkdownText variant="note">{item}</MarkdownText>
            </li>
          ))}
        </ol>
      ) : (
        <div className={className}>
          {items.map((item, i) => (
            <div className={tone === 'warning' ? 'warning-note' : undefined} key={i}>
              <MarkdownText variant="note">{item}</MarkdownText>
            </div>
          ))}
        </div>
      )}
    </section>
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
            <div className="evidence-sidebar__subtitle">按引用编号追踪完整条文与官方来源</div>
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
                <div className="evidence-sidebar__group-note">{group.scope_note ?? '直接展示本组可核查来源，不展开相邻条文。'}</div>
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
  const label = item.citation_label ?? item.title;
  const lawStatus = LAW_STATUS_LABELS[item.law_status] ?? '状态未知';
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
      aria-label={`${item.citation_ref} ${label}`}
    >
      <button
        type="button"
        className="evidence-card__trigger"
        onClick={() => onSelect(item.citation_ref, label)}
        aria-expanded={selected}
      >
        <span className="evidence-card__top">
          <span className="evidence-card__index">{item.citation_ref}</span>
          <span className="evidence-card__usage">{lawStatus}</span>
        </span>
        <span className="evidence-card__title">{label}</span>
        <span className="evidence-card__source">{item.title}</span>
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
            <div><span>发布机关</span><strong>{item.issuing_body || '未提供'}</strong></div>
            <div><span>法律状态</span><strong className={item.law_status === 'effective' ? 'is-current' : 'is-warning'}>{lawStatus}</strong></div>
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
