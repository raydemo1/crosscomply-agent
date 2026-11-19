import { useEffect, useMemo, useState, type CSSProperties } from 'react';
import { Link } from 'react-router-dom';
import { ChevronRight, Search } from 'lucide-react';
import { listCases, listMyRemediations } from '../api/client';
import type { CaseSummaryApi, CaseStatus, WorkbenchUser } from '../types/api';
import { CASE_STATUS_LABELS } from '../utils/workflow';
import { formatTime, relativeTime } from '../utils/display';
import CaseCenterHeader from './CaseCenterHeader';
import { RiskSeal } from './ReportFigures';

const GROUPS: Record<string, CaseStatus[]> = {
  draft: ['draft'], running: ['pending_review', 'review_running'],
  needs_info: ['needs_info'], verification: ['pending_source_verification'],
  approval: ['pending_feishu_approval'], finished: ['approved', 'conditionally_approved', 'rejected'], failed: ['run_failed'],
};

// 常驻四个阶段；草稿、待法规核验、审查失败只在有案件时出现。
const OVERVIEW_STAGES: Array<{ key: string; label: string; optional?: boolean; alert?: boolean }> = [
  { key: 'draft', label: '草稿', optional: true },
  { key: 'running', label: '审查中' },
  { key: 'needs_info', label: '待补充核实' },
  { key: 'verification', label: '待法规核验', optional: true },
  { key: 'approval', label: '待审批' },
  { key: 'finished', label: '已结束' },
  { key: 'failed', label: '审查失败', optional: true, alert: true },
];
const RISK_ORDER = ['high', 'medium', 'low', 'unrated'] as const;
const RISK_SHORT: Record<(typeof RISK_ORDER)[number], string> = { high: '高风险', medium: '中风险', low: '低风险', unrated: '未定级' };

function riskKey(item: CaseSummaryApi): (typeof RISK_ORDER)[number] {
  return !item.risk_level || item.risk_level === 'insufficient_evidence' ? 'unrated' : item.risk_level;
}

/** 案件概况：各阶段案件数与风险分布，点一下即按该项筛选。 */
function CaseOverview({ cases, stage, risk, onStage, onRisk }: {
  cases: CaseSummaryApi[]; stage: string; risk: string;
  onStage: (stage: string) => void; onRisk: (risk: string) => void;
}): JSX.Element | null {
  if (!cases.length) return null;
  const stages = OVERVIEW_STAGES
    .map((item) => ({ ...item, count: cases.filter((entry) => GROUPS[item.key].includes(entry.status)).length }))
    .filter((item) => !item.optional || item.count || stage === item.key);
  const risks = RISK_ORDER.map((key) => ({ key, count: cases.filter((item) => riskKey(item) === key).length })).filter((item) => item.count);
  const summary = risks.map((item) => `${RISK_SHORT[item.key]} ${item.count} 件`).join('，');
  return <section className="case-overview" aria-label="案件概况">
    <div className="stat-tabs" style={{ '--stat-columns': stages.length + 1 } as CSSProperties}>
      <button type="button" className="stat-tab" aria-pressed={!stage} onClick={() => onStage('')}>
        <span>全部案件</span><strong>{cases.length}</strong>
      </button>
      {stages.map((item) => <button type="button" key={item.key} className={'stat-tab' + (item.alert ? ' is-alert' : '') + (item.count ? '' : ' is-empty')}
        aria-pressed={stage === item.key} onClick={() => onStage(stage === item.key ? '' : item.key)}>
        <span>{item.label}</span><strong>{item.count}</strong>
      </button>)}
    </div>
    <div className="case-overview__risk">
      <span className="case-overview__risk-title">风险分布</span>
      <div className="case-overview__risk-bar" role="img" aria-label={`风险分布：${summary}`} title={summary}>
        {risks.map((item) => <span key={item.key} className={`case-overview__risk-seg is-${item.key}`} style={{ flexGrow: item.count }} />)}
      </div>
      <ul className="case-overview__risk-legend">
        {risks.map((item) => <li key={item.key}><button type="button" aria-pressed={risk === item.key} onClick={() => onRisk(risk === item.key ? '' : item.key)}>
          <span className={`case-overview__swatch is-${item.key}`} aria-hidden="true" />{RISK_SHORT[item.key]}<b>{item.count}</b>
        </button></li>)}
      </ul>
    </div>
  </section>;
}

export default function CaseCenterPage({ user }: { user: WorkbenchUser }): JSX.Element {
  const [cases, setCases] = useState<CaseSummaryApi[]>([]);
  const [assigned, setAssigned] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState('');
  const [risk, setRisk] = useState('');
  const [stage, setStage] = useState('');
  const [relation, setRelation] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [assignmentError, setAssignmentError] = useState(false);
  useEffect(() => {
    let live = true;
    void listCases().then(({ items }) => { if (live) setCases(items); })
      .catch((reason) => { if (live) setError(reason instanceof Error ? reason.message : '无法加载案件'); })
      .finally(() => { if (live) setLoading(false); });
    void listMyRemediations({ scope: 'mine' }).then(({ items }) => {
      if (live) setAssigned(new Set(items.map((item) => item.case_id)));
    }).catch(() => { if (live) setAssignmentError(true); });
    return () => { live = false; };
  }, [user.id]);
  const visible = useMemo(() => cases.filter((item) => {
    const needle = query.trim().toLowerCase();
    return (!needle || `${item.question} ${item.id}`.toLowerCase().includes(needle))
      && (!risk || (risk === 'unrated' ? !item.risk_level || item.risk_level === 'insufficient_evidence' : item.risk_level === risk))
      && (!stage || GROUPS[stage].includes(item.status))
      && (!relation || (relation === 'owner' ? item.owner_id === user.id : assigned.has(item.id)));
  }), [cases, query, risk, stage, relation, assigned, user.id]);
  return <section className="case-center">
    <CaseCenterHeader />
    {!loading && !error ? <CaseOverview cases={cases} stage={stage} risk={risk} onStage={setStage} onRisk={setRisk} /> : null}
    <div className="case-center__filters" role="search" aria-label="筛选案件">
      <label className="case-center__search"><Search size={17} aria-hidden="true" /><input type="search" aria-label="搜索案件" placeholder="搜索案件或编号" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
      <select aria-label="与我相关" value={relation} onChange={(event) => setRelation(event.target.value)}><option value="">全部可见案件</option><option value="owner">我申报的</option><option value="assigned" disabled={assignmentError}>含我负责事项的</option></select>
    </div>
    {!loading && !error && cases.length && visible.length !== cases.length ? <p className="case-center__count" role="status">筛选出 {visible.length} / {cases.length} 个案件</p> : null}
    {assignmentError ? <p className="case-center__error" role="status">暂时无法读取事项分派信息。</p> : null}
    {error ? <div className="error-box" role="alert">{error}</div> : loading ? <p className="case-center__empty" role="status">正在读取案件…</p> : visible.length ? (
      <div className="case-center__table">
        <table aria-label="案件列表" role="table">
          <thead role="rowgroup"><tr role="row">
            <th scope="col" role="columnheader">案件</th>
            <th scope="col" role="columnheader">阶段</th>
            <th scope="col" role="columnheader">风险</th>
            <th scope="col" role="columnheader">待处理</th>
            <th scope="col" role="columnheader">更新于</th>
          </tr></thead>
          <tbody role="rowgroup">{visible.map((item) => (
            <tr key={item.id} role="row">
              <td role="cell"><Link className="case-center__title" to={`/cases/${item.id}`}>
                <span>{item.question || item.title}</span><ChevronRight size={17} aria-hidden="true" />
              </Link></td>
              <td role="cell">{CASE_STATUS_LABELS[item.status]}</td>
              <td role="cell">{item.risk_level ? <RiskSeal level={item.risk_level} size="sm" /> : <span className="case-center__meta">未定级</span>}</td>
              <td role="cell"><span className="case-center__pending-label" aria-hidden="true">待处理 </span><span className="case-center__number">{item.pending_followup_count || '—'}</span></td>
              <td role="cell"><time dateTime={item.updated_at} title={formatTime(item.updated_at)}>{relativeTime(item.updated_at)}</time></td>
            </tr>
          ))}</tbody>
        </table>
      </div>
    ) : <div className="case-center__empty">
      <p>{cases.length ? '没有符合条件的案件' : '暂无案件'}</p>
      {cases.length ? <button type="button" className="btn-secondary" onClick={() => { setQuery(''); setRisk(''); setStage(''); setRelation(''); }}>清除筛选</button> : <Link className="btn-primary" to="/workbench">新建案件</Link>}
    </div>}
  </section>;
}
