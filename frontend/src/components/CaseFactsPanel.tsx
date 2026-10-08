import type { FactLedgerEntryApi, UserRole } from '../types/api';
import { CheckCircle2, Files, TriangleAlert, MessageCircle, ChevronDown } from 'lucide-react';
import type { SavedCase } from '../types/case';
import { caseFactLedger, factLabel, factValue } from '../utils/factFields';
import './CaseFacts.css';

export default function CaseFactsPanel({ saved, viewerRole }: { saved: SavedCase; viewerRole: UserRole }) {
  const task = saved.reviewTask;
  const ledger = caseFactLedger(saved);
  if (task && (saved.materialSnapshot?.id !== task.material_snapshot_id || saved.intakeSnapshot?.id !== task.intake_snapshot_id)) return null;
  const intake = saved.intakeSnapshot?.id === task?.intake_snapshot_id ? saved.intakeSnapshot : null;
  const confirmed = ledger.filter((e) => e.source_type === 'confirmed_intake' && e.status !== 'conflicted');
  const conflicted = new Set(ledger.filter((e) => e.status === 'conflicted').map((e) => e.field));
  const confirmedDisplay = confirmed.length ? confirmed : Object.entries(intake?.intake ?? {}).filter(([field, value]) => !conflicted.has(field) && value !== null && value !== '' && value !== 'unknown' && (!Array.isArray(value) || value.length > 0) && (typeof value !== 'object' || Array.isArray(value) || Object.keys(value).length > 0)).map(([field, value]) => ({ field, value, source_type: 'confirmed_intake', source_ref: intake!.id, status: 'confirmed' } as FactLedgerEntryApi));
  const statements = ledger.filter((entry) => entry.source_type === 'applicant_statement');
  const latestStatements = [...new Map(statements.map((entry) => [entry.field, entry])).values()];
  const statementHistory = statements.filter((entry) => !latestStatements.includes(entry));
  const sections = [
    { title: '存在不同说法', note: '材料与已确认事实不一致，需要核对。', kind: 'conflict', icon: TriangleAlert, entries: ledger.filter((e) => e.status === 'conflicted') },
    { title: '补充说明待确认', note: '最近的补充内容，尚未成为已确认事实。', kind: 'statement', icon: MessageCircle, entries: latestStatements },
    { title: '已确认的案件事实', note: '本次审查采用的冻结填报内容。', kind: 'confirmed', icon: CheckCircle2, entries: confirmedDisplay },
    { title: '材料中的其他信息', note: '由 Agent 从材料中发现，仍需核对。', kind: 'material', icon: Files, entries: ledger.filter((e) => e.source_type === 'material' && e.status !== 'conflicted' && (viewerRole !== 'requester' || !confirmedDisplay.some((c) => c.field === e.field && JSON.stringify(c.value) === JSON.stringify(e.value)))) },
  ];
  if (!sections.some((s) => s.entries.length)) return null;
  return <section className="card case-facts" aria-label="案件事实与来源">
    <div className="case-facts__heading"><span className="case-module-kicker">判断的事实基础</span><h2>案件事实与核对</h2><p>区分已确认内容、材料发现和补充说明，避免把尚未核实的信息当作事实。</p></div>
    {sections.filter((s) => s.entries.length).map((section) => <details className={`case-facts__group case-facts__group--${section.kind}`} key={`${task?.id}-${section.kind}`} open={section.kind === 'conflict' || section.kind === 'statement' && task?.status !== 'waiting_input'}>
      <summary><section.icon size={19} aria-hidden="true" /><span><strong>{section.title}</strong><small>{section.note}</small></span><span className="case-facts__count">{section.entries.length} 条</span><ChevronDown className="case-disclosure-chevron" size={16} aria-hidden="true" /></summary>
      <FactValues entries={section.entries} viewerRole={viewerRole} />
      {section.kind === 'statement' && statementHistory.length > 0 ? <details className="case-facts__history"><summary>此前的补充说明 · {statementHistory.length} 条</summary><FactValues entries={statementHistory} viewerRole={viewerRole} /></details> : null}
    </details>)}
  </section>;
}

function FactValues({ entries, viewerRole }: { entries: FactLedgerEntryApi[]; viewerRole: UserRole }) {
  return <dl className="case-facts__values">
    {entries.map((entry, index) => <div key={`${entry.field}-${entry.source_ref}-${index}`}>
      <dt>{factLabel(entry.field)}</dt>
      <dd>
        {factValue(entry.field, entry.value)}
        {entry.status === 'conflicted' ? <small>{entry.source_type === 'material' ? '材料中的信息' : '此前确认的填报内容'}</small> : null}
        {viewerRole !== 'requester' ? <details className="case-facts__source">
          <summary>查看来源记录</summary>
          <code>{entry.source_type} · {entry.status}</code>
          <small>{entry.source_ref}</small>
        </details> : null}
      </dd>
    </div>)}
  </dl>;
}
