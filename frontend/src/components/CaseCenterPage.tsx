import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, Search } from 'lucide-react';
import { listCases, listMyRemediations } from '../api/client';
import type { CaseSummaryApi, CaseStatus, WorkbenchUser } from '../types/api';
import { CASE_STATUS_LABELS } from '../utils/workflow';
import { relativeTime, RISK_LABELS } from '../utils/display';
import '../styles/case-center.css';

const GROUPS: Record<string, CaseStatus[]> = {
  draft: ['draft'], running: ['pending_review', 'review_running'],
  needs_info: ['needs_info'], verification: ['pending_source_verification'],
  approval: ['pending_feishu_approval'], finished: ['approved', 'conditionally_approved', 'rejected'], failed: ['run_failed'],
};

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
    <header className="case-center__heading"><h1 className="page-title">案件中心</h1><span>{loading ? '加载中' : `${visible.length} 个案件`}</span></header>
    <div className="case-center__filters">
      <label className="case-center__search"><Search size={17} /><input type="search" aria-label="搜索案件" placeholder="搜索案件或编号" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
      <select aria-label="风险等级" value={risk} onChange={(event) => setRisk(event.target.value)}><option value="">全部风险</option><option value="high">高风险</option><option value="medium">中风险</option><option value="low">低风险</option><option value="unrated">未定级</option></select>
      <select aria-label="案件阶段" value={stage} onChange={(event) => setStage(event.target.value)}><option value="">全部阶段</option><option value="draft">草稿</option><option value="running">审查中</option><option value="needs_info">待补充核实</option><option value="verification">待法规核验</option><option value="approval">待审批</option><option value="finished">已结束</option><option value="failed">审查失败</option></select>
      <select aria-label="与我相关" value={relation} onChange={(event) => setRelation(event.target.value)}><option value="">全部可见案件</option><option value="owner">我申报的</option><option value="assigned" disabled={assignmentError}>含我负责事项的</option></select>
    </div>
    {assignmentError ? <p className="case-center__error" role="status">暂时无法读取事项分派信息。</p> : null}
    {error ? <div className="error-box" role="alert">{error}</div> : loading ? <p role="status">正在读取案件…</p> : <>
      <div className="case-center__table"><table><thead><tr><th>案件</th><th>阶段</th><th>风险</th><th>待处理</th><th>更新于</th><th><span className="sr-only">打开</span></th></tr></thead><tbody>{visible.map((item) => <tr key={item.id}><td><Link className="case-center__title" to={`/cases/${item.id}`}>{item.question || item.title}</Link><small>{item.id}</small></td><td>{CASE_STATUS_LABELS[item.status]}</td><td><span className={`case-center__risk case-center__risk--${item.risk_level ?? 'pending'}`}>{item.risk_level ? RISK_LABELS[item.risk_level] : '未定级'}</span></td><td>{item.pending_followup_count || '—'}</td><td>{relativeTime(item.updated_at)}</td><td><Link className="case-center__open" to={`/cases/${item.id}`} aria-label={`打开案件：${item.question}`}><ArrowRight size={18} /></Link></td></tr>)}</tbody></table></div>
      {!visible.length ? <div className="case-center__empty"><p>{cases.length ? '没有符合条件的案件' : '暂无案件'}</p>{cases.length ? <button type="button" className="btn-link" onClick={() => { setQuery(''); setRisk(''); setStage(''); setRelation(''); }}>清除筛选</button> : <Link className="btn-primary" to="/workbench">新建案件</Link>}</div> : null}
    </>}
  </section>;
}
