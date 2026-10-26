import RiskBadge from './RiskBadge';
import MarkdownText from './MarkdownText';
import ReportHeading from './ReportHeading';
import { partitionReportContent } from '../utils/reportContent';
import type { ReviewResult } from '../types/api';
import '../styles/report-conclusion.css';

interface ReportConclusionProps {
  result: ReviewResult;
  chapterNo: string;
  onCitationClick: (citationRef: string) => void;
}

export default function ReportConclusion({ result, chapterNo, onCitationClick }: ReportConclusionProps) {
  const { body, conditions, usageNotes } = partitionReportContent(result.conclusion, result.risk_boundaries);
  const reasons = Array.from(new Set(result.trigger_reasons.map((reason) => reason.trim()).filter(Boolean)));
  return <section className="case-conclusion report-section report-conclusion" id="report-conclusion">
    <ReportHeading chapterNo={chapterNo} title="审查结论" action={<RiskBadge level={result.risk_level} />} />
    <div className="report-conclusion__summary" aria-label="审查摘要">
      <span className="report-conclusion__summary-label">审查摘要</span>
      <p className="report-conclusion__lead">{result.decision_summary}</p>
      {result.legal_path ? <div className="report-conclusion__path"><span>当前适用路径</span><strong>{result.legal_path}</strong></div> : null}
    </div>
    {reasons.length ? <section className="report-conclusion__points" aria-label="判断要点">
      <h3 className="report-conclusion__section-title">判断要点</h3>
      <ul>{reasons.map((reason) => <li key={reason}><MarkdownText variant="note" allowRawHtml={false}>{reason}</MarkdownText></li>)}</ul>
    </section> : null}
    {body ? <section className="report-conclusion__analysis" aria-label="完整分析">
      <h3 className={/^#{1,6}\s/mu.test(body) ? 'sr-only' : 'report-conclusion__section-title'}>完整分析</h3>
      <MarkdownText variant="report" className="case-conclusion__body" onCitationClick={onCitationClick}>{body}</MarkdownText>
    </section> : null}
    {conditions.length ? <section className="report-conclusion__boundaries" aria-label="结论适用边界">
      <h3>结论适用边界</h3>
      <ul>{conditions.map((condition) => <li key={condition}><MarkdownText variant="note" allowRawHtml={false}>{condition}</MarkdownText></li>)}</ul>
    </section> : null}
    {usageNotes.length ? <aside className="report-conclusion__usage" aria-label="使用说明"><h3>使用说明</h3>{usageNotes.map((note) => <p key={note}>{note}</p>)}</aside> : null}
  </section>;
}
