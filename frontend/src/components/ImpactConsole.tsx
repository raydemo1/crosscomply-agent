import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ApiError, dismissRegulationImpact, listKnowledgeChanges, listRegulationImpacts, startRegulationRereview } from '../api/client';
import type { ImpactSeverity, ImpactStatus, KnowledgeChange, RegulationFlag } from '../types/operations';
import '../styles/operations.css';

const statusLabels: Record<ImpactStatus, string> = {
  open: '待处理', dismissed: '已忽略', rereview_started: '复审中', resolved: '已结案',
};
const severityLabels: Record<ImpactSeverity, string> = { blocking: '阻断送审', upcoming: '即将生效', informational: '提示' };
const changeLabels: Record<KnowledgeChange['change_type'], string> = {
  added: '新增', body_changed: '正文变更', metadata_changed: '元数据变更', removed: '移除', restored: '恢复',
};
const reasons: Record<RegulationFlag['reason'], string> = {
  cited_text_changed: '引用条文正文已变更', source_removed: '法源已移除', source_repealed: '法源已失效或修订',
  superseded: '已有新版本生效', superseded_upcoming: '新版本尚未生效',
};

export default function ImpactConsole({ selected }: { selected: string | null }) {
  const [status, setStatus] = useState<ImpactStatus>('open');
  const [severity, setSeverity] = useState<ImpactSeverity | ''>('');
  const [changes, setChanges] = useState<KnowledgeChange[]>([]);
  const [flags, setFlags] = useState<RegulationFlag[] | null>(null);
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true;
    void Promise.all([listKnowledgeChanges(), listRegulationImpacts(status, severity)]).then(([events, impacts]) => {
      if (active) { setChanges(events.items); setFlags(impacts.items); setError(''); }
    }).catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : '无法读取法规变更影响'); });
    return () => { active = false; };
  }, [status, severity, revision]);

  return <div className="ops-page">
    <header className="ops-page__header"><h1 className="page-title">法规变更影响</h1></header>
    {error ? <p className="ops-error" role="alert">{error}</p> : null}
    <section className="ops-panel ops-history" aria-label="变更事件">
      <div className="ops-section-title"><h2>变更事件</h2></div>
      {changes.length ? <ol className="ops-review__body">
        {changes.map((event) => <li key={event.id}>
          <time dateTime={event.created_at}>{event.created_at.slice(0, 10)}</time>
          {' · '}{event.detail.title}{' · '}{changeLabels[event.change_type]}
          {[...new Set(event.detail.changed_articles.map((article) => article.article_no).filter(Boolean))].map((number) => ` · ${number}`)}
          {' · '}{event.impact_status === 'computed' ? `影响 ${event.flagged_cases} 个案件` : event.impact_status === 'failed' ? '影响计算失败' : '待计算'}
        </li>)}
      </ol> : <p className="ops-empty ops-empty--compact">暂无变更事件</p>}
    </section>
    <section className="ops-panel ops-history" aria-label="影响提示">
      <div className="ops-section-title"><h2>影响提示</h2></div>
      <div className="ops-toolbar">
        <label className="ops-field">处理状态<select value={status} onChange={(event) => { setFlags(null); setStatus(event.target.value as ImpactStatus); }}>
          {Object.entries(statusLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
        <label className="ops-field">影响级别<select value={severity} onChange={(event) => { setFlags(null); setSeverity(event.target.value as ImpactSeverity | ''); }}>
          <option value="">全部</option>{Object.entries(severityLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
      </div>
      {!flags?.length ? <p className="ops-empty">{flags ? '当前筛选下没有影响提示' : error ? '无法读取影响提示' : '正在读取影响提示…'}</p>
        : <table className="ops-diff">
          <thead><tr><th scope="col">案件</th><th scope="col">法规</th><th scope="col">影响</th><th scope="col">处理</th></tr></thead>
          <tbody>{flags.map((flag) => <tr key={flag.id} className={selected === flag.id ? 'impact-selected' : undefined}>
            <th scope="row"><Link className="ops-link" to={`/cases/${flag.case_id}`}>{flag.case_title}</Link>
              {flag.activity_title ? <div className="ops-secondary">{flag.activity_title}</div> : null}</th>
            <td>{flag.title}{flag.article_nos.length ? ` · ${flag.article_nos.join('、')}` : ''}</td>
            <td><span className="ops-tag ops-tag--pending">{severityLabels[flag.severity]}</span><p>{reasons[flag.reason]}</p>
              {flag.effective_on ? <time dateTime={flag.effective_on}>{flag.effective_on}</time> : null}</td>
            <td>{flag.status === 'open' ? <ImpactActions flag={flag} onDone={() => setRevision((value) => value + 1)} />
              : <>{flag.rereview_case_id ? <Link className="ops-link" to={`/cases/${flag.rereview_case_id}`}>查看复审</Link> : statusLabels[flag.status]}
                {flag.resolution_note ? <p className="ops-secondary">{flag.resolution_note}</p> : null}</>}</td>
          </tr>)}</tbody>
        </table>}
    </section>
  </div>;
}

function ImpactActions({ flag, onDone }: { flag: RegulationFlag; onDone: () => void }) {
  const [dismissing, setDismissing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [error, setError] = useState('');
  const [needsRegistration, setNeedsRegistration] = useState(false);
  async function act(dismiss: boolean) {
    setBusy(true);
    setError('');
    setNeedsRegistration(false);
    try {
      if (dismiss) await dismissRegulationImpact(flag.id, note.trim());
      else await startRegulationRereview(flag.id);
      onDone();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '操作失败，请重试');
      if (reason instanceof ApiError && (reason.detail as { code?: string } | null)?.code === 'register_transfer_first') {
        setNeedsRegistration(true);
      }
    } finally {
      setBusy(false);
    }
  }
  return <>
    <div className="ops-review__links">
      <button type="button" className="btn-secondary" disabled={busy} onClick={() => setDismissing(!dismissing)}>忽略</button>
      <button type="button" className="btn-secondary" disabled={busy} onClick={() => void act(false)}>发起复审</button>
    </div>
    {dismissing ? <form className="ops-impact--form" onSubmit={(event) => { event.preventDefault(); void act(true); }}>
      <label className="ops-field">忽略说明<textarea required maxLength={1600} value={note} onChange={(event) => setNote(event.target.value)} /></label>
      <button type="submit" className="btn-primary" disabled={busy || !note.trim()}>确认忽略</button>
    </form> : null}
    {error ? <p className="ops-error" role="alert">{error}</p> : null}
    {needsRegistration ? <Link className="ops-link" to={`/cases/business?register=${flag.case_id}`}>登记为持续出境业务</Link> : null}
  </>;
}
