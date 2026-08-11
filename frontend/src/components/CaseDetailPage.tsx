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

import { useCallback, useMemo, useState } from 'react';
import type { CaseStatus, CitationGroup, CitationUsage, RetrievalHit, ReviewApiResponse, ReviewFacts } from '../types/api';
import { isReviewFailedResponse } from '../types/api';
import type { CitationVerdict, SavedCase } from '../types/case';
import { setActionStatus, setCitationVerdict } from '../store/caseStore';
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

      <CaseOperations saved={saved} onStatusChange={onStatusChange} />

      {failed ? (
        <FailedChain response={response} />
      ) : (
        <ReviewChain
          saved={completedSaved}
          onVerdictChange={handleVerdict}
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

function CaseOperations({ saved, onStatusChange }: { saved: SavedCase; onStatusChange: (caseId: string, status: CaseStatus) => void }): JSX.Element {
  const openActions = saved.actions.filter((action) => action.status !== 'completed').length;
  return (
    <section className="card case-operations">
      <div className="case-operations__heading"><div><div className="report-kicker">案件流程</div><h2>从证据到行动</h2></div><span className={`status-chip status-chip--${saved.status}`}>{statusLabel(saved.status)}</span></div>
      <div className="case-operations__stats"><span><strong>{saved.actions.length}</strong> 项整改动作</span><span><strong>{openActions}</strong> 项待处理</span><span><strong>{saved.events.length}</strong> 条审计记录</span></div>
      {saved.actions.length > 0 ? (
        <div className="case-operations__list">
          {saved.actions.map((action) => (
            <div className="case-action-row" key={action.id}>
              <div><strong>{action.title}</strong><span>{action.description}</span></div>
              <button type="button" className={`action-status action-status--${action.status}`} onClick={() => setActionStatus(action.id, action.status === 'completed' ? 'open' : 'completed')}>
                {action.status === 'completed' ? '已完成' : '标记完成'}
              </button>
            </div>
          ))}
        </div>
      ) : null}
      <div className="case-operations__actions">
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
}

function ReviewChain({ saved, onVerdictChange }: ReviewChainProps): JSX.Element {
  const response = saved.response as Extract<ReviewApiResponse, { review_case_id: string }>;
  const result = response.review_result;
  const facts = response.review_facts;
  const selfCheck = response.evidence_self_check;
  const queries = response.retrieval_queries ?? [];
  const evidenceChunks = response.evidence_chunks ?? [];
  const verdicts = saved.feedback?.citationVerdicts ?? {};
  const [selectedEvidenceId, setSelectedEvidenceId] = useState<string | null>(null);
  const [evidenceAnnouncement, setEvidenceAnnouncement] = useState('');

  const evidenceCount = evidenceChunks.length;
  const citationCount = useMemo(
    () => response.citation_groups.reduce((sum, g) => sum + g.citations.length, 0),
    [response.citation_groups],
  );

  const handleEvidenceSelect = useCallback((chunkId: string, label: string) => {
    setSelectedEvidenceId(chunkId);
    setEvidenceAnnouncement(`已定位到引用依据：${label}`);

    window.requestAnimationFrame(() => {
      const target = document.getElementById(`evidence-${cssId(chunkId)}`);
      if (!target) return;
      target.focus({ preventScroll: true });
      target.scrollIntoView({
        behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches
          ? 'auto'
          : 'smooth',
        block: 'nearest',
      });
    });
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
            <MarkdownText variant="report" className="case-conclusion__body">
              {result.conclusion}
            </MarkdownText>
            <GroundedClaims
              claims={result.claims}
              evidenceChunks={evidenceChunks}
              compact
              selectedEvidenceId={selectedEvidenceId}
              onEvidenceSelect={handleEvidenceSelect}
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
            <summary>展开完整证据包与人工反馈</summary>
            <div className="report-disclosure__body">
              <CitationList
                groups={response.citation_groups}
                evidenceChunks={evidenceChunks}
                sourceEvidencePackets={response.source_evidence_packets}
                verdicts={verdicts}
                onVerdictChange={onVerdictChange}
              />
              <FeedbackPanel saved={saved} />
            </div>
          </details>
        </main>

        <EvidenceSidebar
          groups={response.citation_groups}
          evidenceChunks={evidenceChunks}
          selectedEvidenceId={selectedEvidenceId}
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
  selectedEvidenceId,
}: {
  groups: CitationGroup[];
  evidenceChunks: RetrievalHit[];
  selectedEvidenceId: string | null;
}): JSX.Element {
  const chunks = useMemo(() => {
    const map = new Map<string, RetrievalHit>();
    evidenceChunks.forEach((chunk) => map.set(chunk.chunk_id, chunk));
    return map;
  }, [evidenceChunks]);
  const citations = groups.flatMap((group) =>
    group.citations.map((citation) => ({
      ...citation,
      groupUsage: group.usage,
      chunk: chunks.get(citation.chunk_id),
    })),
  );

  // Split into legal basis (citable) vs auxiliary (non-citable) groups.
  const legalCitations = citations.filter((c) => c.can_cite_clause);
  const auxCitations = citations.filter((c) => !c.can_cite_clause);

  return (
    <aside className="evidence-sidebar" aria-label="引用依据">
      <div className="evidence-sidebar__head">
        <div className="evidence-sidebar__title">引用依据</div>
        <div className="evidence-sidebar__count">{citations.length} 条</div>
      </div>
      {citations.length === 0 ? (
        <div className="state-block__hint">暂无可引用依据。</div>
      ) : (
        <div className="evidence-sidebar__list">
          {legalCitations.length > 0 && (
            <div className="evidence-sidebar__group evidence-sidebar__group--primary">
              <div className="evidence-sidebar__group-title">法律依据</div>
              {legalCitations.map((item, index) => (
                <EvidenceCard
                  key={item.chunk_id}
                  item={item}
                  index={index}
                  selected={selectedEvidenceId === item.chunk_id}
                />
              ))}
            </div>
          )}
          {auxCitations.length > 0 && (
            <div className="evidence-sidebar__group evidence-sidebar__group--auxiliary">
              <div className="evidence-sidebar__group-title">辅助证据</div>
              {auxCitations.map((item, index) => (
                <EvidenceCard
                  key={item.chunk_id}
                  item={item}
                  index={index}
                  auxiliary
                  selected={selectedEvidenceId === item.chunk_id}
                />
              ))}
            </div>
          )}
        </div>
      )}
    </aside>
  );
}

function EvidenceCard({
  item,
  index,
  auxiliary = false,
  selected = false,
}: {
  item: {
    chunk_id: string;
    citation_label: string | null;
    title: string;
    groupUsage: CitationUsage;
    chunk?: RetrievalHit;
  };
  index: number;
  auxiliary?: boolean;
  selected?: boolean;
}): JSX.Element {
  return (
    <article
      className={
        'evidence-card' +
        (auxiliary ? ' evidence-card--aux' : '') +
        (selected ? ' is-selected' : '')
      }
      id={`evidence-${cssId(item.chunk_id)}`}
      tabIndex={-1}
      aria-label={`${USAGE_LABELS[item.groupUsage]}：${item.citation_label ?? item.title}`}
    >
      <div className="evidence-card__top">
        <span className="evidence-card__index">[{shortId(item.chunk_id)}]</span>
        <span className="evidence-card__usage">{USAGE_LABELS[item.groupUsage]}</span>
      </div>
      <div className="evidence-card__title">
        {index + 1}. {item.citation_label ?? item.title}
      </div>
      {item.chunk ? (
        <p className="evidence-card__text">{item.chunk.text}</p>
      ) : (
        <p className="evidence-card__muted">该证据正文不可用。</p>
      )}
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
