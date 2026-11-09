import RiskBadge from './RiskBadge';
import { ChevronDown } from 'lucide-react';
import MarkdownText from './MarkdownText';
import ReportHeading from './ReportHeading';
import type { ReviewResult } from '../types/api';
import '../styles/report-conclusion.css';

interface ReportConclusionProps {
  result: ReviewResult;
  conditions: readonly string[];
}

export default function ReportConclusion({ result, conditions }: ReportConclusionProps) {
  const reasons = Array.from(new Set(result.trigger_reasons.map((reason) => reason.trim()).filter(Boolean)));
  return <section className="case-conclusion report-section report-conclusion" id="report-conclusion">
    <ReportHeading title="审查结论" action={<RiskBadge level={result.risk_level} />} />
    <div className="report-conclusion__summary" aria-label="审查摘要">
      <p className="report-conclusion__lead">{result.decision_summary}</p>
      {result.legal_path ? <dl className="report-conclusion__path"><dt>当前适用路径</dt><dd>{result.legal_path}</dd></dl> : null}
    </div>
    {conditions.length ? <section className="report-conclusion__boundaries" aria-label="结论适用前提与范围">
      <h3>结论适用前提与范围</h3>
      <ul>{conditions.map((condition) => <li key={condition}><MarkdownText variant="note" allowRawHtml={false}>{condition}</MarkdownText></li>)}</ul>
    </section> : null}
    {reasons.length ? <details className="report-conclusion__points" open={!result.issues?.length}>
      <summary>判断要点 <ChevronDown className="case-disclosure-chevron" size={15} aria-hidden="true" /></summary>
      <ul>{reasons.map((reason) => <li key={reason}><MarkdownText variant="note" allowRawHtml={false}>{reason}</MarkdownText></li>)}</ul>
    </details> : null}
  </section>;
}

interface ReportAnalysisProps {
  body: string;
  onCitationClick: (citationRef: string) => void;
}

export function ReportAnalysis({ body, onCitationClick }: ReportAnalysisProps) {
  if (!body) return null;

  return <section className="report-section report-analysis" id="report-analysis">
    <ReportHeading title="完整分析" />
    <MarkdownText variant="report" className="case-conclusion__body" onCitationClick={onCitationClick}>{body}</MarkdownText>
  </section>;
}
