import { useEffect, useState, type CSSProperties } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, CheckCircle2, History, RefreshCw } from 'lucide-react';
import { listWorkInbox } from '../api/client';
import type { WorkItem, WorkKind } from '../types/operations';

const GROUP_TABS = [
  { key: 'supplement', label: '申报补充' },
  { key: 'review', label: '审查与审批' },
  { key: 'remediation', label: '整改与验收' },
];

const KIND_LABEL: Record<WorkKind, string> = {
  supplement: '补充事实',
  review: '案件审查',
  source_review: '法规复核',
  remediation: '整改事项',
  remediation_review: '整改验收',
  approval: '待送审',
  transfer_draft: '变更申报',
  transfer_due: '业务复核',
  impact_review: '影响确认',
  portfolio_threshold: '主体累计',
};

function getGroup(kind: WorkKind): string {
  if (kind === 'remediation' || kind === 'remediation_review') return 'remediation';
  if (kind === 'supplement' || kind === 'transfer_draft') return 'supplement';
  return 'review';
}

export default function WorkInboxPage(): JSX.Element {
  const [items, setItems] = useState<WorkItem[] | null>(null);
  const [error, setError] = useState('');
  const [filter, setFilter] = useState('all');
  const [refresh, setRefresh] = useState(0);
  useEffect(() => {
    let active = true;
    const load = () => void listWorkInbox().then((data) => {
      if (active) { setItems(data.items); setError(''); }
    }).catch((reason: unknown) => {
      if (active) setError(reason instanceof Error ? reason.message : '无法读取待办');
    });
    load();
    const timer = window.setInterval(load, 30_000);
    return () => { active = false; window.clearInterval(timer); };
  }, [refresh]);
  const all = items ?? [];
  const due = all.filter((item) => item.urgency).length;
  const visible = all.filter((item) => filter === 'all' || (filter === 'due' ? Boolean(item.urgency) : getGroup(item.kind) === filter));
  // 同一案件的事项归在一起，案件问题只写一次。
  const groups: Array<{ context: string; items: WorkItem[] }> = [];
  for (const item of visible) {
    const group = groups.find((entry) => entry.context === item.context);
    if (group) group.items.push(item);
    else groups.push({ context: item.context, items: [item] });
  }
  // 组内先列阻塞结论的补充事项，再列审查审批，最后是整改。
  const order = (item: WorkItem) => ['supplement', 'review', 'remediation'].indexOf(getGroup(item.kind));
  for (const group of groups) group.items.sort((a, b) => order(a) - order(b));
  // 所有事项负责人相同或都没有期限时，这一列没有区分度，不显示。
  const owners = new Set(visible.map((item) => item.owner_name));
  const showOwner = owners.size > 1;
  const showDue = visible.some((item) => item.due_on);
  const gridClass = 'ops-work-grid' + (showOwner ? '' : ' no-owner') + (showDue ? '' : ' no-due');
  const tabs = [
    { key: 'all', label: '全部待办', count: all.length },
    ...GROUP_TABS.map((tab) => ({ ...tab, count: all.filter((item) => getGroup(item.kind) === tab.key).length })),
    ...(due || filter === 'due' ? [{ key: 'due', label: '临近到期或逾期', count: due, alert: true }] : []),
  ];

  return <section className="ops-page">
    <header className="ops-page__header">
      <h1 className="page-title">我的待办</h1>
      <div className="ops-actions">
        <Link className="btn-secondary ops-button" to="/reviews/remediation"><History size={16} />整改记录</Link>
        <button type="button" className="btn-secondary ops-icon-button" aria-label="刷新待办" title="刷新待办" onClick={() => setRefresh((value) => value + 1)}><RefreshCw size={17} /></button>
      </div>
    </header>
    {items ? <div className="stat-tabs" role="group" aria-label="按类别筛选待办" style={{ '--stat-columns': tabs.length } as CSSProperties}>
      {tabs.map((tab) => <button type="button" key={tab.key} aria-pressed={filter === tab.key}
        className={'stat-tab' + ('alert' in tab && tab.alert ? ' is-alert' : '') + (tab.count ? '' : ' is-empty')}
        onClick={() => setFilter(filter === tab.key && tab.key !== 'all' ? 'all' : tab.key)}>
        <span>{tab.label}</span><strong>{tab.count}</strong>
      </button>)}
    </div> : null}
    <section className="ops-panel" aria-label="待处理事项">
      {error ? <p className="ops-error" role="alert">{error}</p> : null}
      {items === null ? (!error && <p className="ops-empty" role="status">正在读取待办…</p>) : visible.length ? (
        <div className="ops-work-list">
          {showOwner || showDue ? <div className={'ops-list-head ' + gridClass} aria-hidden="true"><span>事项</span>{showOwner ? <span>负责人</span> : null}{showDue ? <span>期限</span> : null}<span>下一步</span></div> : null}
          {groups.map((group) => <section className="ops-work-group" key={group.context || 'none'} aria-label={group.context || '其他事项'}>
            {group.context ? <h2 className="ops-work-group__title">{group.context}<span>{group.items.length} 项</span></h2> : null}
            <ul>{group.items.map((item) => <li className={gridClass + ' ops-work-item'} key={item.id}>
              <div className="ops-item-main">
                <span className="ops-item-meta"><span className="ops-tag">{KIND_LABEL[item.kind]}</span>{item.reason}</span>
                <strong>{item.title}</strong>
              </div>
              {showOwner ? <span className="ops-work-owner">{item.owner_name}</span> : null}
              {showDue ? (item.due_on ? <span className={item.urgency ? 'ops-deadline is-' + item.urgency : 'ops-deadline'}>
                <time dateTime={item.due_on}>{item.due_on}</time>
                {item.urgency ? <small>{item.urgency === 'overdue' ? '已逾期' : '即将到期'}</small> : null}
              </span> : <span className="ops-secondary ops-no-due">—</span>) : null}
              <Link className="ops-link ops-row-action" to={item.href}>{item.action}<ArrowRight size={16} aria-hidden="true" /></Link>
            </li>)}</ul>
          </section>)}
        </div>
      ) : <div className="ops-empty"><CheckCircle2 size={27} /><p>{filter === 'all' ? '当前没有需要处理的事项' : '这个分类暂无待办'}</p></div>}
    </section>
  </section>;
}
