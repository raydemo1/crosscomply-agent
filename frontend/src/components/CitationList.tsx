/**
 * CitationList — reviewer/admin governance view.
 *
 * The report displays the article itself. Source metadata and human
 * verification controls belong in the case records.
 */

import { useMemo, useState } from 'react';
import type { CitationGroup, RetrievalHit, ReviewResult, UserRole } from '../types/api';
import type { CitationVerdict } from '../types/case';
import {
  AUTHORITY_LABELS,
  CITATION_ROLE_LABELS,
  citationDisplayLabel,
  DOC_TYPE_LABELS,
  LAW_STATUS_LABELS,
  USAGE_LABELS,
  USAGE_ORDER,
} from '../utils/display';

interface CitationListProps {
  groups: CitationGroup[];
  evidenceChunks: RetrievalHit[] | undefined;
  claims: ReviewResult['claims'];
  verdicts: Record<string, CitationVerdict>;
  readOnly?: boolean;
  onVerdictChange: (chunkId: string, verdict: CitationVerdict | null) => void;
  viewerRole: UserRole;
}

export default function CitationList({
  groups,
  evidenceChunks,
  claims,
  verdicts,
  readOnly = false,
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
  const claimsByCitation = useMemo(() => {
    const result = new Map<string, number[]>();
    for (const [index, claim] of (claims ?? []).entries()) {
      for (const ref of claim.supporting_citation_refs ?? []) {
        const indices = result.get(ref) ?? [];
        indices.push(index + 1);
        result.set(ref, indices);
      }
    }
    return result;
  }, [claims]);

  if (ordered.length === 0) {
    return <div className="state-block__hint">暂无可评价引用。</div>;
  }

  return (
    <div className="cite-list cite-list--governance">
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
                claimIndices={claimsByCitation.get(citation.citation_ref)}
                chunk={chunkMap.get(citation.chunk_id)}
                verdict={verdicts[citation.chunk_id] ?? null}
                readOnly={readOnly}
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
  claimIndices,
  chunk,
  verdict,
  readOnly,
  onVerdictChange,
  viewerRole,
}: {
  citation: CitationGroup['citations'][number];
  claimIndices?: number[];
  chunk: RetrievalHit | undefined;
  verdict: CitationVerdict | null;
  readOnly: boolean;
  onVerdictChange: (chunkId: string, verdict: CitationVerdict | null) => void;
  viewerRole: UserRole;
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const label = citationDisplayLabel(citation);
  return (
    <div className={'cite-row cite-row--governance' + (open ? ' is-open' : '')}>
      <button type="button" className="cite-row__head" onClick={() => setOpen((value) => !value)} aria-expanded={open}>
        <span className="cite-row__chevron" aria-hidden="true">{open ? '▾' : '▸'}</span>
        <span className="cite-row__label">{label}</span>
      </button>
      {open ? (
        <div className="cite-row__body">
          <div className="cite-row__meta cite-row__meta--authority">
            <span><span className="cite-row__meta-label">效力层级</span>{AUTHORITY_LABELS[citation.authority] ?? citation.authority}</span>
            <span><span className="cite-row__meta-label">角色</span>{CITATION_ROLE_LABELS[citation.citation_role] ?? '引用角色未提供'}</span>
            <span><span className="cite-row__meta-label">发布机关</span>{citation.issuing_body || '未提供'}</span>
            <span><span className="cite-row__meta-label">效力状态</span>{LAW_STATUS_LABELS[citation.law_status] ?? LAW_STATUS_LABELS.unknown}</span>
            {citation.doc_type ? <span><span className="cite-row__meta-label">依据类型</span>{DOC_TYPE_LABELS[citation.doc_type] ?? citation.doc_type}</span> : null}
            {citation.publish_date ? <span><span className="cite-row__meta-label">发布日期</span>{citation.publish_date}</span> : null}
            {citation.effective_date ? <span><span className="cite-row__meta-label">生效日期</span>{citation.effective_date}</span> : null}
            {claimIndices?.length ? <span><span className="cite-row__meta-label">支持的结论</span>{claimIndices.join('、')}</span> : null}
          </div>
          {citation.source_url ? <a className="btn-link" href={citation.source_url} target="_blank" rel="noreferrer">打开来源原文 <span aria-hidden="true">↗</span></a> : null}
          {viewerRole === 'admin' && chunk ? (
            <div className="cite-row__meta">
              <span><span className="cite-row__meta-label">文本片段</span><code>{chunk.chunk_id}</code></span>
              <span><span className="cite-row__meta-label">检索方式</span><code>{chunk.retriever}</code></span>
              <span><span className="cite-row__meta-label">检索得分</span><code>{chunk.score.toFixed(4)}</code></span>
            </div>
          ) : null}
          {readOnly || viewerRole === 'requester' ? null : <div className="cite-row__feedback">
            <span className="cite-row__field-label">人工引用评价</span>
            <div className="cite-row__verdicts">
              <button
                type="button"
                aria-pressed={verdict === 'correct'}
                className={'cite-verdict' + (verdict === 'correct' ? ' is-active is-correct' : '')}
                onClick={() => onVerdictChange(citation.chunk_id, verdict === 'correct' ? null : 'correct')}
              >
                正确
              </button>
              <button
                type="button"
                aria-pressed={verdict === 'wrong'}
                className={'cite-verdict' + (verdict === 'wrong' ? ' is-active is-wrong' : '')}
                onClick={() => onVerdictChange(citation.chunk_id, verdict === 'wrong' ? null : 'wrong')}
              >
                需复核
              </button>
            </div>
          </div>}
        </div>
      ) : null}
    </div>
  );
}
