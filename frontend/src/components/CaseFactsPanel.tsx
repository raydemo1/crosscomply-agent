import type { FactLedgerEntryApi, UserRole } from '../types/api';
import { ArrowRight, CheckCircle2, Files, TriangleAlert, MessageCircle, ChevronDown } from 'lucide-react';
import type { SavedCase } from '../types/case';
import { caseFactLedger, factLabel, factValue } from '../utils/factFields';

export default function CaseFactsPanel({ saved, viewerRole, onOpenDocument }: { saved: SavedCase; viewerRole: UserRole; onOpenDocument?: () => void }) {
  const task = saved.reviewTask;
  const ledger = caseFactLedger(saved);
  const aligned = !task || (saved.materialSnapshot?.id === task.material_snapshot_id && saved.intakeSnapshot?.id === task.intake_snapshot_id);
  const intake = aligned && saved.intakeSnapshot?.id === task?.intake_snapshot_id ? saved.intakeSnapshot : null;
  const confirmed = ledger.filter((e) => e.source_type === 'confirmed_intake' && e.status !== 'conflicted');
  const conflicted = new Set(ledger.filter((e) => e.status === 'conflicted').map((e) => e.field));
  const confirmedDisplay = confirmed.length ? confirmed : Object.entries(intake?.intake ?? {}).filter(([field, value]) => !conflicted.has(field) && value !== null && value !== '' && value !== 'unknown' && (!Array.isArray(value) || value.length > 0) && (typeof value !== 'object' || Array.isArray(value) || Object.keys(value).length > 0)).map(([field, value]) => ({ field, value, source_type: 'confirmed_intake', source_ref: intake!.id, status: 'confirmed' } as FactLedgerEntryApi));
  const statements = ledger.filter((entry) => entry.source_type === 'applicant_statement');
  const latestStatements = [...new Map(statements.map((entry) => [entry.field, entry])).values()];
  const statementHistory = statements.filter((entry) => !latestStatements.includes(entry));
  const sections = [
    { title: '信息冲突', kind: 'conflict', icon: TriangleAlert, entries: ledger.filter((e) => e.status === 'conflicted') },
    { title: '补充说明待确认', kind: 'statement', icon: MessageCircle, entries: latestStatements },
    { title: '已确认的案件事实', kind: 'confirmed', icon: CheckCircle2, entries: confirmedDisplay },
    { title: '材料中的其他信息', kind: 'material', icon: Files, entries: ledger.filter((e) => e.source_type === 'material' && e.status !== 'conflicted' && (viewerRole !== 'requester' || !confirmedDisplay.some((c) => c.field === e.field && JSON.stringify(c.value) === JSON.stringify(e.value)))) },
  ];
  const populatedSections = sections.filter((section) => section.entries.length);
  if (!populatedSections.length && !onOpenDocument) return null;
  return <section className="card case-facts" aria-labelledby="case-facts-title">
    <header className="case-panel-heading">
      <h2 id="case-facts-title">案件事实</h2>
      {onOpenDocument ? <button type="button" className="btn-link case-view-link" onClick={onOpenDocument}>核对原文 <ArrowRight size={14} aria-hidden="true" /></button> : null}
    </header>
    {populatedSections.length === 0 ? <p className="case-facts__empty">{aligned ? '本次审查暂无可展示的结构化事实。' : '当前审查与送审版本未匹配，暂无法展示事实。'}</p> : null}
    {populatedSections.map((section) => <details className={`case-facts__group case-facts__group--${section.kind}`} key={`${task?.id}-${section.kind}`} open={section.kind === 'confirmed' || section.kind === 'conflict' || (section.kind === 'statement' && task?.status !== 'waiting_input')}>
      <summary><section.icon size={19} aria-hidden="true" /><span><strong>{section.title}</strong></span><span className="case-facts__count">{new Set(section.entries.map((entry) => entry.field)).size} 项</span><ChevronDown className="case-disclosure-chevron" size={16} aria-hidden="true" /></summary>
      <FactValues entries={section.entries} viewerRole={viewerRole} />
      {section.kind === 'statement' && statementHistory.length > 0 ? <details className="case-facts__history"><summary>此前的补充说明 · {statementHistory.length} 条</summary><FactValues entries={statementHistory} viewerRole={viewerRole} /></details> : null}
    </details>)}
  </section>;
}

const FACT_SOURCE_LABELS = {
  confirmed_intake: '已确认的申报信息',
  material: '送审材料',
  applicant_statement: '申报人补充说明',
};

function FactValues({ entries, viewerRole }: { entries: FactLedgerEntryApi[]; viewerRole: UserRole }) {
  const groups = new Map<string, FactLedgerEntryApi[]>();
  for (const entry of entries) {
    const group = groups.get(entry.field) ?? [];
    group.push(entry);
    groups.set(entry.field, group);
  }
  return <dl className="case-facts__values">
    {[...groups].map(([field, group]) => <div key={field}>
      <dt>{factLabel(field)}</dt>
      <dd>{group.map((entry, index) => <div className="case-facts__value" key={`${entry.source_ref}-${index}`}>
        <span>{factValue(field, entry.value)}</span>
        {entry.status === 'conflicted' ? <small>{FACT_SOURCE_LABELS[entry.source_type]}</small> : null}
        {viewerRole !== 'requester' ? <details className="case-facts__source">
          <summary aria-label="查看来源记录">来源</summary>
          {entry.status !== 'conflicted' ? <span>{FACT_SOURCE_LABELS[entry.source_type]}</span> : null}
          <small>{entry.source_ref}</small>
        </details> : null}
      </div>)}</dd>
    </div>)}
  </dl>;
}
