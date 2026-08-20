import { useMemo } from 'react';
import type { ReactNode } from 'react';
import type { Citation, GroundedClaim, RetrievalHit } from '../types/api';
import MarkdownText from './MarkdownText';
import { citationDisplayLabel } from '../utils/display';

interface GroundedClaimsProps {
  claims: GroundedClaim[] | undefined;
  evidenceChunks: RetrievalHit[] | undefined;
  citations: Citation[];
  compact?: boolean;
  headerAction?: ReactNode;
  onEvidenceSelect?: (citationRef: string, label: string) => void;
  onCitationClick?: (citationRef: string) => void;
}

export default function GroundedClaims({
  claims,
  evidenceChunks,
  citations,
  compact = false,
  headerAction,
  onEvidenceSelect,
  onCitationClick,
}: GroundedClaimsProps): JSX.Element | null {
  const chunkMap = useMemo(() => {
    const map = new Map<string, RetrievalHit>();
    (evidenceChunks ?? []).forEach((chunk) => map.set(chunk.chunk_id, chunk));
    return map;
  }, [evidenceChunks]);

  if (!claims || claims.length === 0) return null;

  return (
    <section
      className={'grounded-claims' + (compact ? ' grounded-claims--compact' : '')}
      aria-label="关键判断与引用依据"
    >
      <div className="grounded-claims__header">
        <div className="grounded-claims__header-copy">
          <span>关键判断与引用</span>
          <span>点击编号查看条文</span>
        </div>
        {headerAction ? <div className="grounded-claims__header-action">{headerAction}</div> : null}
      </div>
      {claims.map((claim, index) => {
        const citationRefs = claim.supporting_citation_refs ?? [];
        return (
          <article
            className="grounded-claim"
            key={`${claim.text}-${index}`}
          >
            <div className="grounded-claim__text">
              <span className="grounded-claim__index" aria-hidden="true">
                {index + 1}
              </span>
              <MarkdownText variant="inline" onCitationClick={onCitationClick}>
                {claim.text}
              </MarkdownText>
            </div>
            <div className="grounded-claim__refs" aria-label={`判断 ${index + 1} 的引用依据`}>
              {citationRefs.map((citationRef) => {
                const citation = citations.find((item) => item.citation_ref === citationRef);
                const chunk = citation ? chunkMap.get(citation.chunk_id) : undefined;
                const label = citation ? citationDisplayLabel(citation) : '引用依据未找到';

                return onEvidenceSelect ? (
                  <button
                    type="button"
                    className="grounded-claim__ref"
                    key={citationRef}
                    onClick={() => onEvidenceSelect(citationRef, label)}
                    aria-label={`在引用依据栏查看：${label}`}
                  >
                    <span className="grounded-claim__ref-id">{label}</span>
                  </button>
                ) : (
                  <a
                    className="grounded-claim__ref"
                    key={citationRef}
                    href={`#evidence-${cssId(citationRef)}`}
                    aria-label={`在引用依据栏查看：${label}`}
                  >
                    <span className="grounded-claim__ref-id">{label}</span>
                  </a>
                );
              })}
            </div>
          </article>
        );
      })}
    </section>
  );
}

export function cssId(value: string): string {
  return value.replace(/[^a-zA-Z0-9_-]/g, '-');
}
