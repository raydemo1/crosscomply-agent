import { useCallback, useEffect, useMemo, useState } from 'react';
import type {
  RemediationAssigneeApi,
  RemediationPlanApi,
  RemediationPlanCreatePayload,
  RemediationPriority,
  RemediationTaskApi,
  RemediationTaskStatus,
  WorkbenchUser,
} from '../types/api';
import {
  activateRemediationPlan,
  createRemediationPlan,
  getRemediationTask,
  getRemediationPlan,
  listAssignableUsers,
  reviewRemediationSubmission,
  startRemediationTask,
  submitRemediationTask,
  updateRemediationTask,
} from '../api/client';
import { validateUploadFile } from '../api/client';
import { formatTime, relativeTime } from '../utils/display';
import './RemediationPlanPage.css';

const STATUS_LABELS: Record<RemediationTaskStatus, string> = {
  open: '待处理',
  in_progress: '处理中',
  pending_review: '待复核',
  completed: '已完成',
};

const PRIORITY_LABELS: Record<RemediationPriority, string> = { high: '高优先级', medium: '中优先级', low: '低优先级' };

export interface RemediationPlanPageProps {
  caseId: string;
  user: WorkbenchUser;
  onBack?: () => void;
  /** Optional server result for previews/tests; production loads by caseId. */
  initialPlan?: RemediationPlanApi | null;
  /** Review suggestions selected by the reviewer when no plan exists yet. */
  recommendations?: string[];
}

export interface MyRemediationsPageProps {
  user: WorkbenchUser;
  onBack?: () => void;
  initialItems?: RemediationTaskApi[];
}

interface TaskDetailProps {
  task: RemediationTaskApi;
  user: WorkbenchUser;
  assignableUsers: RemediationAssigneeApi[];
  onChanged: () => Promise<void>;
}

interface DraftTask {
  title: string;
  description: string;
  source_recommendation_index: number | null;
  priority: RemediationPriority;
  assignee_id: string;
  due_date: string;
}

const EMPTY_DRAFT: DraftTask = {
  title: '',
  description: '',
  source_recommendation_index: null,
  priority: 'medium',
  assignee_id: '',
  due_date: '',
};

function canManage(user: WorkbenchUser): boolean {
  return user.role === 'reviewer' || user.role === 'admin';
}

function isOverdue(task: RemediationTaskApi): boolean {
  return Boolean(task.due_date && task.status !== 'completed' && task.due_date < new Date().toISOString().slice(0, 10));
}

function taskAssignee(task: RemediationTaskApi, users: RemediationAssigneeApi[]): RemediationAssigneeApi | null {
  return task.assignee ?? users.find((item) => item.id === task.assignee_id) ?? null;
}

function latestSubmission(task: RemediationTaskApi) {
  return task.latest_submission ?? task.submissions?.[task.submissions.length - 1] ?? null;
}

function planCounts(plan: RemediationPlanApi) {
  if (plan.counts) return plan.counts;
  const tasks = plan.tasks;
  return {
    total: tasks.length,
    open: tasks.filter((item) => item.status === 'open').length,
    in_progress: tasks.filter((item) => item.status === 'in_progress').length,
    pending_review: tasks.filter((item) => item.status === 'pending_review').length,
    completed: tasks.filter((item) => item.status === 'completed').length,
    overdue: tasks.filter(isOverdue).length,
  };
}

function taskStatusClass(status: RemediationTaskStatus): string {
  return `remediation-status remediation-status--${status}`;
}

function planStatusLabel(status: RemediationPlanApi['status']): string {
  return { draft: '待建立', active: '执行中', completed: '已完成', cancelled: '已取消' }[status];
}

export default function RemediationPlanPage({ caseId, user, onBack, initialPlan = null, recommendations = [] }: RemediationPlanPageProps): JSX.Element {
  const [plan, setPlan] = useState<RemediationPlanApi | null>(initialPlan);
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(initialPlan?.tasks[0]?.id ?? null);
  const [assignableUsers, setAssignableUsers] = useState<RemediationAssigneeApi[]>([]);
  const [loading, setLoading] = useState(initialPlan === undefined);
  const [error, setError] = useState<string | null>(null);
  const [showBuilder, setShowBuilder] = useState(false);
  const [activating, setActivating] = useState(false);

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const next = await getRemediationPlan(caseId);
      setPlan(next);
      setSelectedTaskId((current) => current && next.tasks.some((task) => task.id === current) ? current : next.tasks[0]?.id ?? null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法加载整改计划');
    } finally {
      setLoading(false);
    }
  }, [caseId]);

  useEffect(() => {
    if (initialPlan === null) void refresh();
  }, [initialPlan, refresh]);

  useEffect(() => {
    if (!canManage(user)) return;
    void listAssignableUsers().then((result) => setAssignableUsers(result.items)).catch(() => setAssignableUsers([]));
  }, [user]);

  const selectedTask = plan?.tasks.find((task) => task.id === selectedTaskId) ?? null;
  const counts = plan ? planCounts(plan) : null;

  if (loading) {
    return <section className="remediation-page"><div className="card remediation-state"><strong>正在加载整改计划…</strong></div></section>;
  }

  if (!plan) {
    return (
      <section className="remediation-page">
        <RemediationPageTop title="建立整改计划" onBack={onBack} />
        <div className="card remediation-empty-plan">
          <div className="remediation-empty-plan__mark" aria-hidden="true">＋</div>
          <h1>这个案件还没有整改计划</h1>
          <p>从审查建议中明确选择需要交接的事项，再分派给具体负责人。审查建议不会自动变成任务。</p>
          {canManage(user) ? <button type="button" className="remediation-button remediation-button--primary" onClick={() => setShowBuilder(true)}>建立整改计划</button> : <span className="remediation-muted">等待审核人建立计划</span>}
        </div>
        {showBuilder ? <PlanBuilder caseId={caseId} user={user} recommendations={recommendations} assignableUsers={assignableUsers} onCreated={(created) => { setPlan(created); setShowBuilder(false); setSelectedTaskId(created.tasks[0]?.id ?? null); }} onCancel={() => setShowBuilder(false)} /> : null}
        {error ? <div className="remediation-error" role="alert">{error}</div> : null}
      </section>
    );
  }

  return (
    <section className="remediation-page">
      <RemediationPageTop title="案件整改计划" onBack={onBack}>
        <span className={`remediation-plan-status remediation-plan-status--${plan.status}`}>{planStatusLabel(plan.status)}</span>
      </RemediationPageTop>
      <RemediationPlanOverview plan={plan} />
      {plan.status === 'draft' && canManage(user) ? <div className="remediation-plan-activation"><p>计划仍处于草稿，确认负责人和期限后激活，任务才会收到处理入口。</p><button type="button" className="remediation-button remediation-button--primary" disabled={activating} onClick={() => { setActivating(true); void activateRemediationPlan(plan.id).then((next) => setPlan(next)).catch((reason) => setError(reason instanceof Error ? reason.message : '无法激活整改计划')).finally(() => setActivating(false)); }}>{activating ? '正在激活…' : '激活整改计划'}</button></div> : null}
      {error ? <div className="remediation-error" role="alert">{error}</div> : null}
      <div className="remediation-workspace">
        <section className="card remediation-task-list" aria-label="整改任务列表">
          <div className="remediation-section-heading"><div><span className="remediation-kicker">任务清单</span><h2>按状态处理</h2></div><span className="remediation-count">{counts?.total ?? 0} 项</span></div>
          <div className="remediation-task-list__groups">
            {(['open', 'in_progress', 'pending_review', 'completed'] as RemediationTaskStatus[]).map((status) => {
              const tasks = plan.tasks.filter((task) => task.status === status);
              if (tasks.length === 0) return null;
              return <div className="remediation-task-group" key={status}><div className="remediation-task-group__label"><span>{STATUS_LABELS[status]}</span><b>{tasks.length}</b></div>{tasks.map((task) => <button type="button" className={'remediation-task-item' + (selectedTaskId === task.id ? ' is-selected' : '')} key={task.id} onClick={() => setSelectedTaskId(task.id)}><span className={taskStatusClass(task.status)}>{STATUS_LABELS[task.status]}</span><strong>{task.title}</strong><small>{taskAssignee(task, assignableUsers)?.display_name ?? '尚未分派'} · {task.due_date ? `截止 ${task.due_date}` : '未设期限'}</small>{isOverdue(task) ? <em>已逾期</em> : null}</button>)}</div>;
            })}
          </div>
          {plan.tasks.length === 0 ? <div className="remediation-muted">计划中还没有任务。</div> : null}
        </section>
        <section className="card remediation-task-detail" aria-label="整改任务详情">
          {selectedTask ? <RemediationTaskDetail task={selectedTask} user={user} assignableUsers={assignableUsers} onChanged={refresh} /> : <div className="remediation-detail-placeholder">选择左侧任务查看要求、负责人和提交记录。</div>}
        </section>
      </div>
    </section>
  );
}

function RemediationPageTop({ title, onBack, children }: { title: string; onBack?: () => void; children?: React.ReactNode }): JSX.Element {
  return <header className="remediation-page__top"><button type="button" className="remediation-back" onClick={onBack}>← 返回</button><div><span className="remediation-kicker">CrossComply · 执行闭环</span><h1>{title}</h1></div><div className="remediation-page__top-meta">{children}</div></header>;
}

function RemediationPlanOverview({ plan }: { plan: RemediationPlanApi }): JSX.Element {
  const counts = planCounts(plan);
  const progress = counts.total ? Math.round((counts.completed / counts.total) * 100) : 0;
  return <section className="card remediation-overview"><div className="remediation-overview__copy"><span className="remediation-kicker">关联案件</span><h2>{plan.case_title ?? plan.case_id}</h2><p>整改计划独立于审查结论，完成后由审核人验收并关闭。</p></div><div className="remediation-progress"><div className="remediation-progress__value"><strong>{progress}%</strong><span>完成进度</span></div><div className="remediation-progress__track"><span style={{ width: `${progress}%` }} /></div><div className="remediation-progress__stats"><span>{counts.completed} 已完成</span><span>{counts.pending_review} 待复核</span><span className={counts.overdue ? 'is-danger' : ''}>{counts.overdue} 已逾期</span></div></div></section>;
}

function PlanBuilder({ caseId, user, recommendations, assignableUsers, onCreated, onCancel }: { caseId: string; user: WorkbenchUser; recommendations: string[]; assignableUsers: RemediationAssigneeApi[]; onCreated: (plan: RemediationPlanApi) => void; onCancel: () => void }): JSX.Element {
  const [drafts, setDrafts] = useState<DraftTask[]>([{ ...EMPTY_DRAFT }]);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const updateDraft = (index: number, patch: Partial<DraftTask>): void => setDrafts((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ...patch } : item));
  const addDraft = (): void => setDrafts((items) => [...items, { ...EMPTY_DRAFT }]);
  const removeDraft = (index: number): void => setDrafts((items) => items.length > 1 ? items.filter((_, itemIndex) => itemIndex !== index) : items);
  const save = async (): Promise<void> => {
    if (drafts.some((draft) => !draft.title.trim() || !draft.assignee_id || !draft.due_date)) { setError('每项整改任务都需要填写名称、负责人和截止日期。'); return; }
    setSaving(true); setError(null);
    const payload: RemediationPlanCreatePayload = { tasks: drafts.map((draft) => ({ title: draft.title.trim(), description: draft.description.trim(), source_recommendation: draft.source_recommendation_index === null ? null : recommendations[draft.source_recommendation_index] ?? null, source_recommendation_index: draft.source_recommendation_index, assignee_id: draft.assignee_id, priority: draft.priority, due_date: draft.due_date })) };
    try { onCreated(await createRemediationPlan(caseId, payload)); } catch (reason) { setError(reason instanceof Error ? reason.message : '无法建立整改计划'); } finally { setSaving(false); }
  };
  return <div className="card remediation-builder"><div className="remediation-section-heading"><div><span className="remediation-kicker">审核人操作</span><h2>把审查建议变成可交接任务</h2></div><button type="button" className="remediation-close" onClick={onCancel}>取消</button></div><p className="remediation-builder__intro">只有明确选择并补齐负责人、期限的事项，才会进入整改计划。未选中的审查建议仍保留在案件结论中。</p>{drafts.map((draft, index) => <div className="remediation-builder__item" key={index}><div className="remediation-builder__item-head"><strong>任务 {index + 1}</strong>{drafts.length > 1 ? <button type="button" className="remediation-text-button" onClick={() => removeDraft(index)}>移除</button> : null}</div><div className="remediation-form-grid"><label><span>任务名称</span><input value={draft.title} onChange={(event) => updateDraft(index, { title: event.target.value })} placeholder="例如：完成个人信息保护影响评估" /></label><label><span>来源审查建议</span><select value={draft.source_recommendation_index ?? ''} onChange={(event) => updateDraft(index, { source_recommendation_index: event.target.value === '' ? null : Number(event.target.value) })}><option value="">不关联具体建议</option>{recommendations.map((item, recommendationIndex) => <option key={recommendationIndex} value={recommendationIndex}>建议 {recommendationIndex + 1} · {item.slice(0, 32)}</option>)}</select></label><label><span>负责人</span><select value={draft.assignee_id} onChange={(event) => updateDraft(index, { assignee_id: event.target.value })}><option value="">选择真实用户</option>{assignableUsers.map((item) => <option key={item.id} value={item.id}>{item.display_name} · {item.role === 'admin' ? '管理员' : item.role === 'reviewer' ? '审核人' : '申请人'}</option>)}</select></label><label><span>截止日期</span><input type="date" value={draft.due_date} onChange={(event) => updateDraft(index, { due_date: event.target.value })} /></label><label><span>优先级</span><select value={draft.priority} onChange={(event) => updateDraft(index, { priority: event.target.value as RemediationPriority })}><option value="high">高</option><option value="medium">中</option><option value="low">低</option></select></label><label className="remediation-form-grid__wide"><span>完成要求</span><textarea value={draft.description} onChange={(event) => updateDraft(index, { description: event.target.value })} placeholder="说明交付物与验收标准" /></label></div></div>)}<div className="remediation-builder__footer">{error ? <span className="remediation-form-error">{error}</span> : <button type="button" className="remediation-text-button" onClick={addDraft}>＋再加一项</button>}<div><button type="button" className="remediation-button" onClick={onCancel}>取消</button><button type="button" className="remediation-button remediation-button--primary" disabled={saving} onClick={() => void save()}>{saving ? '正在建立…' : '建立并进入执行'}</button></div></div></div>;
}

function RemediationTaskDetail({ task, user, assignableUsers, onChanged }: TaskDetailProps): JSX.Element {
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState('');
  const [links, setLinks] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  const [reviewNote, setReviewNote] = useState('');
  const [message, setMessage] = useState<string | null>(null);
  const manager = canManage(user);
  const assignee = task.assignee_id === user.id || task.assignee?.id === user.id;
  const canSubmit = assignee && task.status === 'in_progress';
  const latest = latestSubmission(task);

  const run = async (operation: () => Promise<unknown>, success: string): Promise<void> => {
    setBusy(true); setMessage(null);
    try { await operation(); await onChanged(); setMessage(success); } catch (reason) { setMessage(reason instanceof Error ? reason.message : '操作失败，请稍后重试'); } finally { setBusy(false); }
  };
  const submit = async (): Promise<void> => {
    if (!note.trim()) { setMessage('请填写本次完成说明。'); return; }
    try { files.forEach(validateUploadFile); } catch (reason) { setMessage(reason instanceof Error ? reason.message : '附件无法上传'); return; }
    if (files.length > 0) { setMessage('当前服务端只接受已上传对象存储的文件证据；本次请先移除附件，或提交链接。'); return; }
    const evidence = links.split(/\r?\n/).map((item) => item.trim()).filter(Boolean).map((uri) => ({ kind: 'link' as const, label: uri, uri }));
    await run(() => submitRemediationTask(task.id, { note: note.trim(), evidence }), '已提交，等待审核人复核。');
    setNote(''); setLinks(''); setFiles([]);
  };
  const review = (decision: 'accepted' | 'rejected'): Promise<void> => run(() => latest ? reviewRemediationSubmission(latest.id, { decision, review_note: reviewNote.trim() || undefined }) : Promise.resolve(), decision === 'accepted' ? '已验收完成。' : '已退回负责人补充。');
  const assigneeName = task.assignee?.display_name ?? task.assignee_id ?? '尚未分派';
  return <div className="remediation-task-detail__inner"><div className="remediation-task-detail__head"><div><span className={taskStatusClass(task.status)}>{STATUS_LABELS[task.status]}</span><h2>{task.title}</h2><p>{task.description || '暂无完成要求。'}</p></div><span className={`remediation-priority remediation-priority--${task.priority}`}>{PRIORITY_LABELS[task.priority]}</span></div><div className="remediation-task-facts"><div><span>负责人</span><strong>{assigneeName}</strong></div><div><span>截止日期</span><strong className={isOverdue(task) ? 'is-danger' : ''}>{task.due_date ?? '未设置'}{isOverdue(task) ? ' · 已逾期' : ''}</strong></div><div><span>创建时间</span><strong>{formatTime(task.created_at)}</strong></div></div>{task.source_recommendation ? <div className="remediation-source"><span>来源审查建议</span><p>{task.source_recommendation}</p></div> : null}{manager ? <TaskAssignment task={task} assignableUsers={assignableUsers} onChanged={onChanged} /> : null}{latest ? <SubmissionCard submission={latest} /> : <div className="remediation-no-submission">负责人尚未提交处理说明。</div>}{assignee && task.status === 'open' ? <div className="remediation-submit"><div className="remediation-section-heading"><div><span className="remediation-kicker">负责人操作</span><h3>开始处理</h3></div></div><p className="remediation-muted">开始处理后，才能提交完成说明和证据。</p><button type="button" className="remediation-button remediation-button--primary" disabled={busy} onClick={() => void run(() => startRemediationTask(task.id), '已开始处理。')}>开始处理</button></div> : null}{canSubmit ? <div className="remediation-submit"><div className="remediation-section-heading"><div><span className="remediation-kicker">负责人操作</span><h3>提交处理结果</h3></div></div><label><span>完成说明</span><textarea value={note} onChange={(event) => setNote(event.target.value)} placeholder="说明完成了什么、还剩什么，以及如何满足验收要求" /></label><label><span>链接（每行一个，可选）</span><textarea className="remediation-submit__links" value={links} onChange={(event) => setLinks(event.target.value)} placeholder="https://…" /></label><label><span>附件（可选）</span><input type="file" multiple onChange={(event) => setFiles(Array.from(event.target.files ?? []))} /></label>{files.length ? <div className="remediation-file-list">{files.map((file) => <span key={`${file.name}-${file.size}`}>{file.name}</span>)}</div> : null}<button type="button" className="remediation-button remediation-button--primary" disabled={busy} onClick={() => void submit()}>{busy ? '正在提交…' : '提交复核'}</button></div> : null}{manager && latest?.status === 'pending_review' ? <div className="remediation-review"><div className="remediation-section-heading"><div><span className="remediation-kicker">审核人操作</span><h3>复核提交</h3></div></div><textarea value={reviewNote} onChange={(event) => setReviewNote(event.target.value)} placeholder="退回时请说明需要补充的内容；验收时可留下备注" /><div className="remediation-review__actions"><button type="button" className="remediation-button" disabled={busy} onClick={() => void review('rejected')}>退回补充</button><button type="button" className="remediation-button remediation-button--primary" disabled={busy} onClick={() => void review('accepted')}>验收完成</button></div></div> : null}{message ? <div className="remediation-message" role="status">{message}</div> : null}</div>;
}

function TaskAssignment({ task, assignableUsers, onChanged }: { task: RemediationTaskApi; assignableUsers: RemediationAssigneeApi[]; onChanged: () => Promise<void> }): JSX.Element {
  const [assignee, setAssignee] = useState(task.assignee?.id ?? '');
  const [dueDate, setDueDate] = useState(task.due_date ?? '');
  const [priority, setPriority] = useState(task.priority);
  const [saving, setSaving] = useState(false);
  const save = async (): Promise<void> => { setSaving(true); try { await updateRemediationTask(task.id, { assignee_id: assignee, due_date: dueDate || null, priority }); await onChanged(); } finally { setSaving(false); } };
  return <div className="remediation-assignment"><div className="remediation-section-heading"><div><span className="remediation-kicker">审核人可调整</span><h3>分派与验收边界</h3></div></div><div className="remediation-assignment__grid"><label><span>负责人</span><select value={assignee} onChange={(event) => setAssignee(event.target.value)}><option value="">未分派</option>{assignableUsers.map((item) => <option key={item.id} value={item.id}>{item.display_name}</option>)}</select></label><label><span>截止日期</span><input type="date" value={dueDate} onChange={(event) => setDueDate(event.target.value)} /></label><label><span>优先级</span><select value={priority} onChange={(event) => setPriority(event.target.value as RemediationPriority)}><option value="high">高</option><option value="medium">中</option><option value="low">低</option></select></label></div><button type="button" className="remediation-text-button" disabled={saving} onClick={() => void save()}>{saving ? '保存中…' : '保存分派'}</button></div>;
}

function SubmissionCard({ submission }: { submission: NonNullable<RemediationTaskApi['latest_submission']> }): JSX.Element {
  return <div className="remediation-submission"><div className="remediation-submission__head"><span>最近一次提交</span><span className={`remediation-submission-status remediation-submission-status--${submission.status}`}>{submission.status === 'pending_review' ? '待复核' : submission.status === 'accepted' ? '已通过' : '需补充'}</span></div><p>{submission.note}</p><small>{submission.submitted_by} · {relativeTime(submission.created_at)}</small>{submission.evidence.length ? <div className="remediation-evidence-list">{submission.evidence.map((item) => <span key={item.id}>{item.kind === 'file' ? '附件' : item.kind === 'link' ? '链接' : '材料'} · {item.label}</span>)}</div> : null}{submission.review_note ? <div className="remediation-submission__review">复核意见：{submission.review_note}</div> : null}</div>;
}

export function MyRemediationsPage({ user, onBack, initialItems }: MyRemediationsPageProps): JSX.Element {
  const [items, setItems] = useState<RemediationTaskApi[]>(initialItems ?? []);
  const [loading, setLoading] = useState(initialItems === undefined);
  const [status, setStatus] = useState<'all' | RemediationTaskStatus>('all');
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(initialItems?.[0]?.id ?? null);
  const [selectedDetail, setSelectedDetail] = useState<RemediationTaskApi | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reviewerView = canManage(user);
  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true); setError(null);
    try {
      const { listMyRemediations } = await import('../api/client');
      const result = await listMyRemediations({ scope: reviewerView ? 'review' : 'mine', ...(status === 'all' ? {} : { status }) });
      setItems(result.items);
      setSelectedTaskId((current) => current && result.items.some((item) => item.id === current) ? current : result.items[0]?.id ?? null);
    } catch (reason) { setError(reason instanceof Error ? reason.message : '无法加载我的整改'); } finally { setLoading(false); }
  }, [reviewerView, status]);
  useEffect(() => { if (initialItems === undefined) void refresh(); }, [initialItems, refresh]);
  useEffect(() => {
    if (!selectedTaskId) { setSelectedDetail(null); return; }
    let mounted = true;
    void getRemediationTask(selectedTaskId).then((result) => { if (mounted) setSelectedDetail(result.task); }).catch(() => { if (mounted) setSelectedDetail(null); });
    return () => { mounted = false; };
  }, [selectedTaskId]);
  const selected = items.find((item) => item.id === selectedTaskId) ?? null;
  const counts = useMemo(() => ({ pending: items.filter((item) => item.status === 'pending_review').length, open: items.filter((item) => item.status === 'open' || item.status === 'in_progress').length, done: items.filter((item) => item.status === 'completed').length }), [items]);
  const tabs: Array<['all' | RemediationTaskStatus, string]> = reviewerView
    ? [['all', '全部'], ['open', '待处理'], ['pending_review', '待我复核'], ['completed', '已完成']]
    : [['all', '全部'], ['open', '待处理'], ['in_progress', '处理中'], ['completed', '已完成']];
  return <section className="remediation-page"><RemediationPageTop title={reviewerView ? '整改复核' : '我的整改'} onBack={onBack}><span className="remediation-inbox-summary">{counts.open} 待处理{reviewerView ? ` · ${counts.pending} 待复核` : ''}</span></RemediationPageTop><div className="remediation-inbox-tabs" role="tablist" aria-label="整改任务筛选">{tabs.map(([key, label]) => <button type="button" role="tab" aria-selected={status === key} className={status === key ? 'is-active' : ''} key={key} onClick={() => setStatus(key)}>{label}</button>)}</div>{error ? <div className="remediation-error" role="alert">{error}</div> : null}{loading ? <div className="card remediation-state">正在加载任务…</div> : <div className="remediation-workspace remediation-workspace--inbox"><section className="card remediation-inbox-list">{items.length ? items.map((item) => <button type="button" className={'remediation-task-item' + (selectedTaskId === item.id ? ' is-selected' : '')} key={item.id} onClick={() => { setSelectedTaskId(item.id); setSelectedDetail(null); }}><span className={taskStatusClass(item.status)}>{STATUS_LABELS[item.status]}</span><strong>{item.title}</strong><small>{item.case_id} · {item.due_date ? `截止 ${item.due_date}` : '未设期限'}</small>{isOverdue(item) ? <em>已逾期</em> : null}</button>) : <div className="remediation-muted">当前没有需要你处理的整改任务。</div>}</section><section className="card remediation-task-detail">{selected ? <RemediationTaskDetail task={selectedDetail ?? selected} user={user} assignableUsers={[]} onChanged={refresh} /> : <div className="remediation-detail-placeholder">选择任务查看详情。</div>}</section></div>}</section>;
}
