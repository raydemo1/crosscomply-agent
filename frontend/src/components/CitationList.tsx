/**
 * CitationList — reviewer/admin governance view.
 *
 * The business-facing citation surface is the right-hand law-source panel.
 * This list intentionally contains only the stable reference, relationship,
 * source status and human feedback controls so it does not duplicate the
 * complete-article detail shown there.
 */

import { useMemo, useState } from 'react';
import type { CitationGroup, RetrievalHit, UserRole } from '../types/api';
import type { CitationVerdict } from '../types/case';
import {
  AUTHORITY_LABELS,
  CITATION_ROLE_LABELS,
  LAW_STATUS_LABELS,
  USAGE_LABELS,
  USAGE_ORDER,
} from '../utils/display';

interface CitationListProps {
  groups: CitationGroup[];
  evidenceChunks: RetrievalHit[] | undefined;
  verdicts: Record<string, CitationVerdict>;
  onVerdictChange: (chunkId: string, verdict: CitationVerdict | null) => void;
  viewerRole: UserRole;
}

export default function CitationList({
  groups,
  evidenceChunks,
  verdicts,
  onVerdictChange,
  viewerRole,
}: CitationListProps): JSX.Element {
  const chunkMap = useMemo(
    () => new Map((evidenceChunks ?? []).map((chunk) => [chunk.chunk_id, chunk])),
    [evidenceChunks],
  );
  const ordered = useMemo(
    () => [...groups].sort((a, b) => USAGE_ORDER.indexOf(a.usage) - USAGE_ORDER.indexOf(b.usage)),
    [groups],
  );

  if (ordered.length === 0) {
    return <div className="state-block__hint">暂无可评价引用。</div>;
  }

  return (
    <div className="cite-list cite-list--governance">
      <div className="cite-list__notice">
        在线核查见右侧法源面板。
      </div>
      {ordered.map((group) => (
        <section className="cite-list__group" key={group.usage}>
          <div className="cite-list__group-head">
            <span>{USAGE_LABELS[group.usage]}</span>
            <span className="cite-list__group-count">{group.citations.length} 条</span>
          </div>
          <div className="cite-list__items">
            {group.citations.map((citation) => (
              <GovernanceRow
                key={citation.citation_ref || citation.chunk_id}
                citation={citation}
                chunk={chunkMap.get(citation.chunk_id)}
                verdict={verdicts[citation.chunk_id] ?? null}
                onVerdictChange={onVerdictChange}
                viewerRole={viewerRole}
              />
            ))}
          </div>
        </section>
      ))}
    </div>
  );
}

function GovernanceRow({
  citation,
  chunk,
  verdict,
  onVerdictChange,
  viewerRole,
}: {
  citation: CitationGroup['citations'][number];
  chunk: RetrievalHit | undefined;
  verdict: CitationVerdict | null;
  onVerdictChange: (chunkId: string, verdict: CitationVerdict | null) => void;
  viewerRole: UserRole;
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const label = citation.citation_label ?? citation.title;
  return (
    <div className={'cite-row cite-row--governance' + (open ? ' is-open' : '')}>
      <button type="button" className="cite-row__head" onClick={() => setOpen((value) => !value)} aria-expanded={open}>
        <span className="cite-row__chevron" aria-hidden="true">{open ? '▾' : '▸'}</span>
        <span className="cite-row__index">{citation.citation_ref || '—'}</span>
        <span className="cite-row__label">{label}</span>
        <span className="cite-chip cite-chip--role">{LAW_STATUS_LABELS[citation.law_status] ?? '状态未知'}</span>
      </button>
      {open ? (
        <div className="cite-row__body">
          <div className="cite-row__meta cite-row__meta--authority">
            <span><span className="cite-row__meta-label">法源</span>{AUTHORITY_LABELS[citation.authority] ?? citation.authority}</span>
            <span><span className="cite-row__meta-label">角色</span>{CITATION_ROLE_LABELS[citation.citation_role] ?? '引用角色未提供'}</span>
            <span><span className="cite-row__meta-label">发布机关</span>{citation.issuing_body || '未提供'}</span>
          </div>
          {viewerRole === 'admin' && chunk ? (
            <div className="cite-row__meta">
              <span><span className="cite-row__meta-label">chunk</span><code>{chunk.chunk_id}</code></span>
              <span><span className="cite-row__meta-label">检索器</span><code>{chunk.retriever}</code></span>
              <span><span className="cite-row__meta-label">评分</span><code>{chunk.score.toFixed(4)}</code></span>
            </div>
          ) : null}
          <div className="cite-row__feedback">
            <span className="cite-row__field-label">人工引用评价</span>
            <div className="cite-row__verdicts">
              <button
                type="button"
                className={'cite-verdict' + (verdict === 'correct' ? ' is-active is-correct' : '')}
                onClick={() => onVerdictChange(citation.chunk_id, verdict === 'correct' ? null : 'correct')}
              >
                正确
              </button>
              <button
                type="button"
                className={'cite-verdict' + (verdict === 'wrong' ? ' is-active is-wrong' : '')}
                onClick={() => onVerdictChange(citation.chunk_id, verdict === 'wrong' ? null : 'wrong')}
              >
                需复核
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
