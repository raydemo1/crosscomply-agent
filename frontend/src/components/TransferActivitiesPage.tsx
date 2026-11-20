import { useCallback, useEffect, useState } from 'react';
import type { FormEvent } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { ArrowLeft, ArrowRight, CalendarDays, ChevronDown, FileCheck, Globe2, Plus, Search } from 'lucide-react';
import {
  confirmTransferImpact, getTransferActivity, listEligibleTransferCases, listTransferActivities,
  registerTransferActivity, startTransferReview, updateTransferReviewDate,
} from '../api/client';
import type { WorkbenchUser } from '../types/api';
import type { EligibleTransferCase, ImpactOutcome, ScopeValue, TransferActivity, TransferDetail, TransferReview } from '../types/operations';
import { CASE_STATUS_LABELS } from '../utils/workflow';
import CaseCenterHeader from './CaseCenterHeader';
import { PortfolioDirectory } from './PortfolioPanel';

const IMPACT_LABELS: Record<ImpactOutcome, string> = {
  scope_changed: '影响原审批范围',
  scope_unchanged: '原审批范围无变化',
};

function errorMessage(reason: unknown): string {
  return reason instanceof Error ? reason.message : '操作失败，请重试';
}

function valueText(value: ScopeValue): string {
  if (Array.isArray(value)) return value.join('、') || '未填写';
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (value === 'unknown') return '待确认';
  return value || '未填写';
}

export default function TransferActivitiesPage({ user }: { user: WorkbenchUser }): JSX.Element {
  const { activityId } = useParams();
  return activityId ? <ActivityDetail key={activityId} id={activityId} user={user} /> : <ActivityList user={user} />;
}

function ActivityList({ user }: { user: WorkbenchUser }): JSX.Element {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [items, setItems] = useState<TransferActivity[] | null>(null);
  const [query, setQuery] = useState('');
  const [error, setError] = useState('');
  const registering = params.has('register');
  const byEntity = params.get('view') === 'entity' || params.has('entity');
  const sourceCaseId = params.get('register') ?? '';
  const openRegistration = () => setParams({ register: '' });
  useEffect(() => {
    let active = true;
    void listTransferActivities().then((result) => { if (active) setItems(result.items); })
      .catch((reason: unknown) => { if (active) setError(errorMessage(reason)); });
    return () => { active = false; };
  }, []);
  const needle = query.trim().toLocaleLowerCase();
  const visible = (items ?? []).filter((item) =>
    [item.title, item.business_entity, item.recipient, item.destination, item.owner_name]
      .some((value) => value.toLocaleLowerCase().includes(needle)),
  );

  return <section className="ops-page">
    <CaseCenterHeader action={!registering ? <button type="button" className="btn-primary ops-button" onClick={openRegistration}><Plus size={17} />登记业务</button> : null} />
    {registering ? <RegisterForm key={sourceCaseId} sourceCaseId={sourceCaseId} canExclude={user.role !== 'requester'} onCancel={() => setParams({}, { replace: true })}
      onCreated={(id) => navigate(`/cases/business/${id}`, { replace: true })} /> : null}
    {!registering ? <div className="ops-actions" aria-label="台账视图">
      <button type="button" className={byEntity ? 'btn-secondary' : 'btn-primary'} aria-pressed={!byEntity} onClick={() => setParams({})}>按业务查看</button>
      <button type="button" className={byEntity ? 'btn-primary' : 'btn-secondary'} aria-pressed={byEntity} onClick={() => setParams({ view: 'entity' })}>按主体查看</button>
    </div> : null}
    {byEntity ? <PortfolioDirectory key={params.get('entity')} selected={params.get('entity')} /> : <section className="ops-panel" aria-label="出境业务列表">
      <div className="ops-toolbar">
        <label className="ops-search"><Search size={17} aria-hidden="true" /><input type="search" placeholder="搜索业务、主体或接收方" aria-label="搜索出境业务" value={query} onChange={(event) => setQuery(event.target.value)} /></label>
        {items && <small className="ops-secondary">{visible.length} 项业务</small>}
      </div>
      {error ? <p className="ops-error" role="alert">{error}</p> : null}
      {items === null ? (!error && <p className="ops-empty">正在读取出境业务…</p>) : visible.length ? (
        <div>
          <div className="ops-list-head ops-transfer-grid" aria-hidden="true"><span>业务名称</span><span>境外接收方</span><span>下次复核</span><span>状态</span></div>
          <ul className="ops-transfer-list">{visible.map((item) => <li key={item.id}>
            <Link className="ops-transfer-grid ops-transfer-row" to={`/cases/business/${item.id}`}>
              <div className="ops-item-main"><strong>{item.title}</strong><small>{item.business_entity} · {item.owner_name}</small></div>
              <div className="ops-item-main"><span>{[item.recipient, item.destination].filter(Boolean).join(' · ') || '未填写'}</span>
                {item.data_types.length ? <small>{item.data_types.join('、')}</small> : null}</div>
              <time dateTime={item.next_review_on}>{item.next_review_on}</time>
              <span className="ops-transfer-status"><span className={'ops-tag ' + (item.active_status ? 'ops-tag--pending' : '')}>
                {item.active_status ? '复审进行中' : CASE_STATUS_LABELS[item.approval.status]}
              </span><ArrowRight size={17} aria-hidden="true" /></span>
            </Link>
          </li>)}</ul>
        </div>
      ) : <div className="ops-empty"><Globe2 size={28} /><p>{items.length ? '没有匹配的业务' : '尚未登记出境业务'}</p>
        {!items.length && !registering ? <button className="btn-secondary" type="button" onClick={openRegistration}>从已审批案件登记</button> : null}
      </div>}
    </section>}
  </section>;
}

function RegisterForm({ sourceCaseId, canExclude, onCancel, onCreated }: {
  sourceCaseId: string; canExclude: boolean; onCancel: () => void; onCreated: (id: string) => void;
}): JSX.Element {
  const [eligible, setEligible] = useState<EligibleTransferCase[] | null>(null);
  const [caseId, setCaseId] = useState('');
  const [title, setTitle] = useState('');
  const [entity, setEntity] = useState('');
  const [date, setDate] = useState('');
  const [included, setIncluded] = useState(true);
  const [exclusionNote, setExclusionNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    void listEligibleTransferCases().then(({ items }) => {
      if (!active) return;
      setEligible(items);
      const source = items.find((item) => item.id === sourceCaseId);
      if (source) { setCaseId(source.id); setTitle(source.title.slice(0, 160)); }
    })
      .catch((reason: unknown) => { if (active) setError(errorMessage(reason)); });
    return () => { active = false; };
  }, [sourceCaseId]);

  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    setBusy(true); setError('');
    try {
      const result = await registerTransferActivity({ case_id: caseId, title: title.trim(), business_entity: entity.trim(), next_review_on: date,
        counts_toward_total: included, exclusion_note: included ? '' : exclusionNote.trim() });
      onCreated(result.id);
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }

  return <form className="ops-panel ops-form" onSubmit={(event) => void submit(event)}>
    <h2>登记出境业务</h2>
    {error ? <p className="ops-error" role="alert">{error}</p> : null}
    {eligible?.length ? <div className="ops-form-grid">
      <label className="ops-field ops-field--wide">已审批案件
        <select required value={caseId} onChange={(event) => {
          const id = event.target.value;
          setCaseId(id);
          setTitle(eligible.find((item) => item.id === id)?.title.slice(0, 160) ?? '');
        }}><option value="">选择案件</option>{eligible.map((item) =>
          <option key={item.id} value={item.id}>{item.title}{item.decided_at ? ` · ${item.decided_at.slice(0, 10)}` : ''}</option>,
        )}</select>
      </label>
      <label className="ops-field">业务名称<input required maxLength={160} value={title} onChange={(event) => setTitle(event.target.value)} /></label>
      <label className="ops-field">业务主体<input required maxLength={160} placeholder="填写开展该业务的公司或机构" value={entity} onChange={(event) => setEntity(event.target.value)} /></label>
      <label className="ops-field">下次复核日期<input type="date" required value={date} onChange={(event) => setDate(event.target.value)} /></label>
      {canExclude ? <label className="ops-field--wide"><input type="checkbox" checked={included} onChange={(event) => setIncluded(event.target.checked)} /> 计入主体累计</label> : null}
      {canExclude && !included ? <label className="ops-field ops-field--wide">排除说明<textarea required maxLength={1600} value={exclusionNote} onChange={(event) => setExclusionNote(event.target.value)} /></label> : null}
    </div> : error ? null : <p className="ops-secondary">{eligible ? '目前没有可登记的已审批案件。' : '正在查找已审批案件…'}</p>}
    <div className="ops-actions"><button type="button" className="btn-secondary" onClick={onCancel} disabled={busy}>取消</button>
      {eligible?.length ? <button type="submit" className="btn-primary" disabled={busy || !caseId || !title.trim() || !entity.trim() || !date}>{busy ? '正在登记…' : '登记业务'}</button> : null}
    </div>
  </form>;
}

function ActivityDetail({ id, user }: { id: string; user: WorkbenchUser }): JSX.Element {
  const navigate = useNavigate();
  const [detail, setDetail] = useState<TransferDetail | null>(null);
  const [error, setError] = useState('');
  const [starting, setStarting] = useState(false);
  const refresh = useCallback(async () => setDetail(await getTransferActivity(id)), [id]);
  useEffect(() => {
    let active = true;
    void getTransferActivity(id).then((result) => { if (active) setDetail(result); })
      .catch((reason: unknown) => { if (active) setError(errorMessage(reason)); });
    return () => { active = false; };
  }, [id]);
  if (!detail) return <section className="ops-page"><Link className="ops-link" to="/cases/business"><ArrowLeft size={16} />返回出境业务</Link>
    {error ? <p className="ops-error" role="alert">{error}</p> : <p className="ops-empty">正在读取出境业务…</p>}
  </section>;
  const { approval } = detail;
  return <section className="ops-page ops-page--detail">
    <Link className="ops-link ops-back" to="/cases/business"><ArrowLeft size={16} />出境业务</Link>
    <header className="ops-page__header">
      <div><h1 className="page-title">{detail.title}</h1><p>{detail.business_entity} · {detail.owner_name}</p></div>
      {!detail.active_status ? <button className="btn-primary ops-button" type="button" onClick={() => setStarting((value) => !value)}><Plus size={17} />发起复审</button> : null}
    </header>
    <div className="ops-detail-grid">
      <section className="ops-panel ops-summary">
        <h2><FileCheck size={19} aria-hidden="true" />最近一次审批<span className="ops-tag ops-tag--success">{CASE_STATUS_LABELS[approval.status]}</span></h2>
        <dl><div><dt>境外接收方</dt><dd>{detail.recipient || '未填写'}</dd></div>
          <div><dt>接收地</dt><dd>{detail.destination || '未填写'}</dd></div>
          <div className="ops-field--wide"><dt>数据类别</dt><dd>{detail.data_types.join('、') || '未填写'}</dd></div></dl>
        <div className="ops-summary__footer">
          {approval.decided_at ? <time dateTime={approval.decided_at}>{approval.decided_at.slice(0, 10)}</time> : null}
          <Link className="ops-link" to={`/cases/${approval.case_id}`}>查看审批案件<ArrowRight size={16} /></Link>
        </div>
      </section>
      <ScheduleForm key={detail.next_review_on} id={id} date={detail.next_review_on}
        onSaved={(value) => setDetail((current) => current ? { ...current, next_review_on: value } : current)} />
    </div>
    {starting ? <StartReviewForm detail={detail} onCancel={() => setStarting(false)} onCreated={({ case_id, owner_id }) => {
      setStarting(false);
      if (owner_id === user.id) navigate(`/workbench?case=${case_id}`);
      else void refresh().catch((reason: unknown) => setError(errorMessage(reason)));
    }} /> : null}
    {error ? <p className="ops-error" role="alert">{error}</p> : null}
    <section className="ops-panel ops-history" aria-labelledby="transfer-reviews">
      <div className="ops-section-title"><h2 id="transfer-reviews">复审记录</h2></div>
      {detail.reviews.length ? detail.reviews.map((review, index) =>
        <ReviewHistory key={review.case_id} activityId={id} review={review} first={index === 0} onConfirmed={refresh} />,
      ) : <p className="ops-empty ops-empty--compact">暂无复审记录</p>}
    </section>
  </section>;
}

function ScheduleForm({ id, date, onSaved }: { id: string; date: string; onSaved: (value: string) => void }): JSX.Element {
  const [next, setNext] = useState(date);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    setBusy(true); setError('');
    try { onSaved((await updateTransferReviewDate(id, next)).next_review_on); }
    catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }
  return <form className="ops-panel ops-summary ops-schedule" onSubmit={(event) => void submit(event)}>
    <h2><CalendarDays size={19} aria-hidden="true" />复核计划</h2>
    <label className="ops-field">下次复核日期<input type="date" required value={next} onChange={(event) => setNext(event.target.value)} /></label>
    {error ? <p className="ops-error" role="alert">{error}</p> : null}
    <button type="submit" className="btn-secondary" disabled={busy || !next || next === date}>{busy ? '正在保存…' : '保存日期'}</button>
  </form>;
}

function StartReviewForm({ detail, onCancel, onCreated }: {
  detail: TransferDetail; onCancel: () => void; onCreated: (result: { case_id: string; owner_id: string }) => void;
}): JSX.Element {
  const [entity, setEntity] = useState(detail.business_entity);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    setBusy(true); setError('');
    try {
      onCreated(await startTransferReview(detail.id, {
        baseline_case_id: detail.approval.case_id, business_entity: entity.trim(), change_note: note.trim(),
      }));
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }
  return <form className="ops-panel ops-form" onSubmit={(event) => void submit(event)}>
    <h2>发起复审</h2>
    <label className="ops-field">本次业务主体<input required maxLength={160} value={entity} onChange={(event) => setEntity(event.target.value)} /></label>
    <label className="ops-field">复审原因<textarea rows={3} required maxLength={1600} placeholder="例如更换境外供应商、调整数据范围或例行复核" value={note} onChange={(event) => setNote(event.target.value)} /></label>
    {error ? <p className="ops-error" role="alert">{error}</p> : null}
    <div className="ops-actions"><button type="button" className="btn-secondary" onClick={onCancel} disabled={busy}>取消</button>
      <button type="submit" className="btn-primary" disabled={busy || !entity.trim() || !note.trim()}>{busy ? '正在创建…' : '创建复审草稿'}</button></div>
  </form>;
}

function ReviewHistory({ activityId, review, first, onConfirmed }: {
  activityId: string; review: TransferReview; first: boolean; onConfirmed: () => Promise<void>;
}): JSX.Element {
  const status = review.status === 'pending_feishu_approval' && !review.impact
    ? '待确认影响'
    : CASE_STATUS_LABELS[review.status];
  return <details className="ops-review" open={first}>
    <summary><span>{review.change_note}</span><span className="ops-tag">{status}</span><ChevronDown size={17} aria-hidden="true" /></summary>
    <div className="ops-review__body">
      <div className="ops-review__links"><Link className="ops-link" to={`/cases/${review.baseline_case_id}`}>查看原审批</Link>
        <Link className="ops-link" to={review.can_edit_draft ? `/workbench?case=${review.case_id}` : `/cases/${review.case_id}`}>
          {review.can_edit_draft ? '补充材料并提交' : '打开复审案件'}<ArrowRight size={16} />
        </Link></div>
      {review.changes.length ? <table className="ops-diff">
        <thead><tr><th scope="col">事项</th><th scope="col">原审批</th><th scope="col">{review.status === 'draft' ? '当前草稿' : '本次复审'}</th></tr></thead>
        <tbody>{review.changes.map((change) => <tr key={change.field}><th scope="row">{change.field}</th><td>{valueText(change.before)}</td><td>{valueText(change.after)}</td></tr>)}</tbody>
      </table> : <p className="ops-secondary">当前记录未显示业务范围的字段变化。{review.status === 'draft' ? '提交新材料并确认事实后会更新对比。' : ''}</p>}
      {review.impact ? <div className="ops-impact">
        <strong>{IMPACT_LABELS[review.impact.outcome]}</strong>
        <p>{review.impact.note}</p>
        <small>{review.impact.reviewer_name ?? '法务审核人'} · {review.impact.confirmed_at.slice(0, 10)}</small>
      </div> : null}
      {review.can_confirm && review.task_id ? <ImpactForm activityId={activityId} caseId={review.case_id} taskId={review.task_id} onConfirmed={onConfirmed} /> : null}
    </div>
  </details>;
}

function ImpactForm({ activityId, caseId, taskId, onConfirmed }: {
  activityId: string; caseId: string; taskId: string; onConfirmed: () => Promise<void>;
}): JSX.Element {
  const [outcome, setOutcome] = useState<ImpactOutcome | ''>('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit(event: FormEvent): Promise<void> {
    event.preventDefault();
    if (!outcome) return;
    setBusy(true); setError('');
    try {
      await confirmTransferImpact(activityId, { case_id: caseId, task_id: taskId, outcome, note: note.trim() });
      await onConfirmed();
    } catch (reason) { setError(errorMessage(reason)); }
    finally { setBusy(false); }
  }
  return <form className="ops-impact ops-impact--form" onSubmit={(event) => void submit(event)}>
    <h3>确认变更影响</h3>
    <label className="ops-field">对原审批范围的影响<select required value={outcome} onChange={(event) => {
      const value = event.target.value as ImpactOutcome | '';
      setOutcome(value);
    }}><option value="">选择判断</option>{Object.entries(IMPACT_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
    <label className="ops-field">确认依据<textarea required rows={2} maxLength={1600} value={note} onChange={(event) => setNote(event.target.value)} /></label>
    {error ? <p className="ops-error" role="alert">{error}</p> : null}
    <button className="btn-primary" type="submit" disabled={busy || !outcome || !note.trim()}>{busy ? '正在保存…' : '确认影响'}</button>
  </form>;
}
