import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, CheckCircle2, Clock3, History, RefreshCw } from 'lucide-react';
import { listWorkInbox } from '../api/client';
import type { WorkItem, WorkKind } from '../types/operations';

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
  const visible = (items ?? []).filter((item) => filter === 'all' || getGroup(item.kind) === filter);
  const due = items?.filter((item) => item.urgency).length ?? 0;

  return <section className="ops-page">
    <header className="ops-page__header">
      <h1 className="page-title">我的待办</h1>
      <div className="ops-actions">
        <Link className="btn-secondary ops-button" to="/reviews/remediation"><History size={16} />整改记录</Link>
        <button type="button" className="btn-secondary ops-icon-button" aria-label="刷新待办" title="刷新待办" onClick={() => setRefresh((value) => value + 1)}><RefreshCw size={17} /></button>
      </div>
    </header>
    <section className="ops-panel" aria-label="待处理事项">
      <div className="ops-toolbar">
        <div className="ops-filters" aria-label="筛选待办">
          {Object.entries({ all: '全部待办', supplement: '申报补充', review: '审查与审批', remediation: '整改与验收' }).map(([key, label]) =>
            <button key={key} type="button" aria-pressed={filter === key} onClick={() => setFilter(key)}>
              {label}{key === 'all' && items ? <span>{items.length}</span> : null}
            </button>,
          )}
        </div>
        {due > 0 ? <span className="ops-due-count"><Clock3 size={15} />{due} 项临近到期或已逾期</span> : null}
      </div>
      {error ? <p className="ops-error" role="alert">{error}</p> : null}
      {items === null ? (!error && <p className="ops-empty" role="status">正在读取待办…</p>) : visible.length ? (
        <div className="ops-work-list">
          <div className="ops-list-head ops-work-grid" aria-hidden="true"><span>事项 / 当前卡点</span><span>负责人</span><span>期限</span><span>下一步</span></div>
          <ul>{visible.map((item) => <li className="ops-work-grid ops-work-item" key={item.id}>
            <div className="ops-item-main">
              <span className="ops-tag">{KIND_LABEL[item.kind]}</span>
              <strong>{item.title}</strong>
              {item.context ? <small>{item.context}</small> : null}
              <p>{item.reason}</p>
            </div>
            <span className="ops-work-owner">{item.owner_name}</span>
            {item.due_on ? <span className={item.urgency ? 'ops-deadline is-' + item.urgency : 'ops-deadline'}>
              <time dateTime={item.due_on}>{item.due_on}</time>
              {item.urgency ? <small>{item.urgency === 'overdue' ? '已逾期' : '即将到期'}</small> : null}
            </span> : <span className="ops-secondary ops-no-due">—</span>}
            <Link className="ops-link ops-row-action" to={item.href}>{item.action}<ArrowRight size={16} aria-hidden="true" /></Link>
          </li>)}</ul>
        </div>
      ) : <div className="ops-empty"><CheckCircle2 size={27} /><p>{filter === 'all' ? '当前没有需要处理的事项' : '这个分类暂无待办'}</p></div>}
    </section>
  </section>;
}
