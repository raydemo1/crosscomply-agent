/**
 * EvidenceDossier — right-hand column (360px).
 *
 * Summarises the evidence backing the most recent review result:
 *   - evidence self-check status (with colored badge)
 *   - whether a second retrieval was triggered
 *   - citation groups grouped by usage category
 *     (法律依据 / 条件依据 / 实施参考 / 政策解读)
 *   - each citation shows title, source link, role and can_cite_clause badge
 *
 * Shows an empty state when no review has been run yet.
 *
 * Reuses the existing `.cite-group` / `.cite-group__head` /
 * `.cite-group__count` and `.state-block` component classes from
 * global.css; panel chrome, evidence-status badges and citation cards
 * are styled via the dossier-specific classes also defined in global.css.
 */

import type {
  Citation,
  CitationUsage,
  ClauseCitationRole,
  EvidenceStatus,
  ReviewApiResponse,
} from '../types/api'
import { isReviewFailedResponse } from '../types/api'
import { cssId } from './GroundedClaims'

interface EvidenceDossierProps {
  /** The most recent review response, or null before any review runs. */
  reviewResponse: ReviewApiResponse | null
}

// ---------------------------------------------------------------------------
// Display label maps (enum -> Chinese)
// ---------------------------------------------------------------------------

const EVIDENCE_STATUS_LABEL: Record<EvidenceStatus, string> = {
  not_checked: '未检查',
  sufficient: '证据充分',
  needs_second_retrieval: '需二次检索',
  insufficient: '证据不足',
}

const EVIDENCE_STATUS_BADGE_CLASS: Record<EvidenceStatus, string> = {
  not_checked: 'badge badge-evidence-notchecked',
  sufficient: 'badge badge-evidence-sufficient',
  needs_second_retrieval: 'badge badge-evidence-second',
  insufficient: 'badge badge-evidence-insufficient',
}

const USAGE_LABEL: Record<CitationUsage, string> = {
  legal_basis: '法律依据',
  conditional_basis: '条件依据',
  implementation_reference: '实施参考',
  policy_explanation: '政策解读',
}

/** Stable display order for citation groups. */
const USAGE_ORDER: CitationUsage[] = [
  'legal_basis',
  'conditional_basis',
  'implementation_reference',
  'policy_explanation',
]

const CITATION_ROLE_LABEL: Record<ClauseCitationRole, string> = {
  primary_legal_basis: '主要法律依据',
  conditional_local_basis: '条件 · 地域依据',
  conditional_industry_basis: '条件 · 行业依据',
  implementation_reference: '实施参考',
  interpretation_auxiliary: '解释辅助',
}

const citationListStyle: React.CSSProperties = {
  display: 'flex',
  flexDirection: 'column',
  marginTop: 'var(--space-sm)',
}

// ---------------------------------------------------------------------------

export default function EvidenceDossier({
  reviewResponse,
}: EvidenceDossierProps): JSX.Element {
  // ---- Empty state ------------------------------------------------------
  if (!reviewResponse) {
    return (
      <aside className="app-dossier dossier">
        <div className="dossier-header">
          <div className="dossier-title">证据档案</div>
        </div>
        <div className="dossier-body">
          <div className="state-block">
            <div className="state-block__title">尚无研究记录</div>
          </div>
        </div>
      </aside>
    )
  }

  if (isReviewFailedResponse(reviewResponse)) {
    return (
      <aside className="app-dossier dossier">
        <div className="dossier-header">
          <div className="dossier-title">证据档案</div>
        </div>
        <div className="dossier-body">
          <div className="state-block">
            <div className="state-block__title">审查未完成</div>
            <div className="state-block__hint">
              {reviewResponse.failed_node}：{reviewResponse.message}
              <br />
              已重试 {reviewResponse.attempts} 次
              {reviewResponse.trace_id ? ` · Trace ${reviewResponse.trace_id}` : ''}
            </div>
          </div>
        </div>
      </aside>
    )
  }

  // ---- Populated state --------------------------------------------------
  const { evidence_self_check, citation_groups, second_retrieval_triggered } =
    reviewResponse
  const status = evidence_self_check.status

  // Order groups by the canonical usage order for a stable layout.
  const orderedGroups = [...citation_groups].sort(
    (a, b) =>
      USAGE_ORDER.indexOf(a.usage) - USAGE_ORDER.indexOf(b.usage),
  )

  return (
    <aside className="app-dossier dossier">
      <div className="dossier-header">
        <div className="dossier-title">证据档案</div>
      </div>

      <div className="dossier-body">
        {/* ---- Evidence self-check summary ---- */}
        <div className="dossier-status">
          <div className="dossier-status-row">
            <span className="dossier-status-label">证据自检</span>
            <span className={EVIDENCE_STATUS_BADGE_CLASS[status]}>
              {EVIDENCE_STATUS_LABEL[status]}
            </span>
          </div>
          <div className="dossier-status-row">
            <span className="dossier-status-label">二次检索</span>
            <span className="dossier-status-value">
              {second_retrieval_triggered ? '已触发' : '未触发'}
            </span>
          </div>
        </div>

        {/* ---- Citation groups by usage ---- */}
        {orderedGroups.length === 0 ? (
          <div className="state-block">
            <div className="state-block__title">暂无可引用证据</div>
            <div className="state-block__hint">本次审查未生成可引用证据。</div>
          </div>
        ) : (
          orderedGroups.map((group) => (
            <section className="dossier-group" key={group.usage}>
              <div className="cite-group">
                <div className="cite-group__head">
                  <span>{USAGE_LABEL[group.usage]}</span>
                  <span className="cite-group__count">
                    {group.citations.length}
                  </span>
                </div>
                {group.scope_note ? (
                  <div className="dossier-group-scope">{group.scope_note}</div>
                ) : null}
                <div style={citationListStyle}>
                  {group.citations.map((citation) => (
                    <CitationItem
                      key={citation.chunk_id}
                      citation={citation}
                    />
                  ))}
                </div>
              </div>
            </section>
          ))
        )}
      </div>
    </aside>
  )
}

// ---------------------------------------------------------------------------
// CitationItem — single citation row
// ---------------------------------------------------------------------------

interface CitationItemProps {
  citation: Citation
}

function CitationItem({ citation }: CitationItemProps): JSX.Element {
  return (
    <div
      className="dossier-citation"
      id={`evidence-${cssId(citation.chunk_id)}`}
      tabIndex={-1}
    >
      <div className="dossier-citation-title">
        {citation.citation_label ?? citation.title}
      </div>
      {citation.source_url ? (
        <a
          className="dossier-citation-link"
          href={citation.source_url}
          target="_blank"
          rel="noopener noreferrer"
        >
          {citation.source_url}
        </a>
      ) : null}
      <div className="dossier-citation-meta">
        <span className="dossier-citation-role">
          {CITATION_ROLE_LABEL[citation.citation_role]}
        </span>
        {citation.can_cite_clause ? (
          <span className="dossier-citation-cite">可引用条文</span>
        ) : (
          <span className="dossier-citation-ref">仅作参考</span>
        )}
      </div>
    </div>
  )
}
