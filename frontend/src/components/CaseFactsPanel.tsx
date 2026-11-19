import type { FactLedgerEntryApi, UserRole } from '../types/api';
import { ArrowRight, CheckCircle2, Files, TriangleAlert, MessageCircle } from 'lucide-react';
import type { SavedCase } from '../types/case';
import { FACT_FIELDS, caseFactLedger, factLabel, factValue } from '../utils/factFields';

// 阅读顺序：接收地紧随境外接收方，其余沿用字段定义顺序。
const FIELD_ORDER: string[] = Object.keys(FACT_FIELDS).filter((field) => field !== 'destination_region')
  .flatMap((field) => field === 'overseas_recipient' ? [field, 'destination_region'] : [field]);

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
  return <section className="card case-facts" aria-label="案件事实">
    {onOpenDocument ? <div className="case-facts__toolbar"><button type="button" className="btn-link case-view-link" onClick={onOpenDocument}>核对原文 <ArrowRight size={14} aria-hidden="true" /></button></div> : null}
    {populatedSections.length === 0 ? <p className="case-facts__empty">{aligned ? '本次审查暂无可展示的结构化事实。' : '当前审查与送审版本未匹配，暂无法展示事实。'}</p> : null}
    {populatedSections.map((section) => <section className={`case-facts__group case-facts__group--${section.kind}`} key={`${task?.id}-${section.kind}`} aria-label={section.title}>
      <h3 className="case-facts__group-title"><section.icon size={17} aria-hidden="true" /><span>{section.title}</span><span className="case-facts__count">{new Set(section.entries.map((entry) => entry.field)).size}</span></h3>
      <FactValues entries={section.entries} viewerRole={viewerRole} />
      {section.kind === 'statement' && statementHistory.length > 0 ? <details className="case-facts__history"><summary>此前的补充说明 · {statementHistory.length} 条</summary><FactValues entries={statementHistory} viewerRole={viewerRole} /></details> : null}
    </section>)}
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
  const order = (field: string): number => { const index = FIELD_ORDER.indexOf(field); return index < 0 ? FIELD_ORDER.length : index; };
  return <dl className="case-facts__values">
    {[...groups].sort(([a], [b]) => order(a) - order(b)).map(([field, group]) => <div key={field}>
      <dt>{factLabel(field)}</dt>
      <dd>{group.map((entry, index) => <div className="case-facts__value" key={`${entry.source_ref}-${index}`}>
        <span title={viewerRole !== 'requester' ? `来源：${FACT_SOURCE_LABELS[entry.source_type]} · ${entry.source_ref}` : undefined}>{factValue(field, entry.value)}</span>
        {entry.status === 'conflicted' ? <small className="case-facts__source">{FACT_SOURCE_LABELS[entry.source_type]}</small> : null}
      </div>)}</dd>
    </div>)}
  </dl>;
}
