import { useEffect, useState } from 'react';
import { Check, ArrowRight, FilePlus2, LoaderCircle } from 'lucide-react';
import type { SavedCase } from '../types/case';
import { answerReviewTask, confirmCaseFacts, recordFactAnswer, waitForReviewTask } from '../api/client';
import { openCase } from '../store/caseStore';
import { FACT_FIELDS, factValue, valuesChanged, type FactField } from '../utils/factFields';
import FactValueInput from './FactValueInput';
import './CaseFacts.css';

export default function AgentQuestionPanel({ saved, requester, busy, execute, onEditMaterial }: {
  saved: SavedCase; requester: boolean; busy: boolean;
  execute: (name: string, action: () => Promise<void>) => Promise<void>;
  onEditMaterial?: () => void;
}) {
  const task = saved.reviewTask;
  const state = task?.agent_state;
  const asked = state?.fact_questions?.map((q) => q.field) ?? [];
  const [chosenField, setChosenField] = useState<FactField | ''>('');
  const fields: FactField[] = asked.length ? asked : state?.fact_answer_revision ? Object.keys(state.pending_fact_values ?? {}) as FactField[] : chosenField ? [chosenField] : [];
  const [answer, setAnswer] = useState('');
  const [values, setValues] = useState<Partial<Record<FactField, unknown>>>({});
  useEffect(() => {
    setAnswer('');
    setChosenField('');
    const next: Partial<Record<FactField, unknown>> = {};
    for (const field of [...new Set([...(state?.fact_questions?.map((q) => q.field) ?? []), ...Object.keys(state?.pending_fact_values ?? {}) as FactField[]])]) {
      const suggested = [...(state?.fact_ledger ?? [])].reverse().find((e) => e.field === field && e.source_type === 'material');
      next[field] = state?.pending_fact_values?.[field] ?? suggested?.value ?? saved.intakeSnapshot?.intake[field] ?? saved.intake[field];
    }
    setValues(next);
  }, [task?.id, state?.gate_id, state?.fact_answer_revision]);
  if (!task || task.status !== 'waiting_input' || !state?.gate_id) return null;
  const binding = { task_id: task.id, material_snapshot_id: task.material_snapshot_id, intake_snapshot_id: task.intake_snapshot_id };
  const pending = state.fact_answer_revision && Object.keys(state.pending_fact_values ?? {}).length > 0;
  const continueTask = async (taskId: string) => {
    await openCase(saved.id);
    await waitForReviewTask(taskId, async () => { await openCase(saved.id); });
    await openCase(saved.id);
  };
  const submit = () => void execute('answer', async () => {
    if (requester && fields.length) {
      await recordFactAnswer(saved.id, { ...binding, gate_id: state.gate_id!, answer: answer.trim() || fields.map((field) => `${FACT_FIELDS[field].label}：${factValue(field, values[field])}`).join('；'), values: Object.fromEntries(fields.map((field) => [field, values[field]])) });
      await openCase(saved.id);
    } else {
      const resumed = await answerReviewTask(task.id, state.gate_id!, answer.trim());
      await continueTask(resumed.id);
    }
    setAnswer('');
  });
  const confirm = () => void execute('confirm-facts', async () => {
    const queued = await confirmCaseFacts(saved.id, {
      ...binding, gate_id: state.gate_id!, answer_revision: state.fact_answer_revision!,
      values: state.pending_fact_values!, confirmed: true,
    });
    await continueTask(queued.task_id);
  });
  const changed = valuesChanged(state.pending_fact_values ?? {}, saved.intakeSnapshot?.intake ?? saved.intake);
  const keepOriginal = () => void execute('answer', async () => {
    const resumed = await answerReviewTask(task.id, state.gate_id!, '已核对上述事实，仍以原冻结事实为准。');
    await continueTask(resumed.id);
  });
  const inputs = <div className="case-fact-question__fields">{fields.map((field) => <label key={field}>
    <span>{FACT_FIELDS[field].label}</span>
    <FactValueInput field={field} value={values[field]} disabled={busy} onChange={(value) => setValues((current) => ({ ...current, [field]: value }))} />
    <small>原填报值：{factValue(field, saved.intakeSnapshot?.intake[field])}</small>
  </label>)}</div>;
  return <section className="card case-fact-question" aria-label="补充与确认事实" aria-busy={busy}>
    <div className="case-fact-question__heading"><div><span className="case-module-kicker">{requester ? '需要你补充' : '审查操作'}</span><h2>{requester && pending ? '核对补充事实' : requester ? '补充案件事实' : '补充审查指引'}</h2></div>
      {requester && fields.length > 0 ? <ol className="case-fact-question__steps" aria-label="事实确认步骤"><li className={!pending ? 'is-current' : 'is-done'}>{pending ? <Check size={14} aria-hidden="true" /> : <span>1</span>}填写</li><li className={pending ? 'is-current' : ''}><span>2</span>确认</li></ol> : null}
    </div>
    <p className="case-fact-question__prompt">{state.pending_question || '请补充案件事实'}</p>
    {requester && pending ? <>
      <p>{changed ? '核对以下内容。确认后将更新案件事实，并基于原有材料自动重新审查。' : '补充值与原填报内容一致，可继续当前审查。如需修改，请展开下方的补充内容。'}</p>
      <dl className="case-facts__values case-fact-question__comparison">{Object.entries(state.pending_fact_values ?? {}).map(([field, value]) => <div key={field}><dt>{FACT_FIELDS[field as FactField].label}</dt><dd><small>补充值 · 待确认</small><strong>{factValue(field, value)}</strong><small>原填报值：{factValue(field, saved.intakeSnapshot?.intake[field as FactField])}</small></dd></div>)}</dl>
      <div className="case-fact-question__footer"><button type="button" className="btn-primary" disabled={busy} onClick={changed ? confirm : keepOriginal}>{busy ? <LoaderCircle size={16} className="case-busy-icon" /> : <Check size={16} />}{changed ? '确认这些事实并继续审查' : '保留原事实，继续当前审查'}</button><small>{changed ? '确认前，补充内容仍未核实。' : '补充值与已确认事实一致。'}</small></div>
      <details className="case-fact-question__alternative"><summary>{changed ? '修改补充内容，或保留原事实' : '修改补充内容'}</summary>{inputs}<button type="button" className="case-header__action-btn" disabled={busy} onClick={submit}>重新记录，供我确认</button>{changed ? <button type="button" className="case-header__action-btn" disabled={busy} onClick={keepOriginal}>保留原事实，继续当前审查</button> : null}</details>
    </> : <>
      {requester && !asked.length && <label>需要更正已确认的业务事实？<select value={chosenField} disabled={busy} onChange={(e) => { const field = e.target.value as FactField | ''; setChosenField(field); if (field) setValues((current) => ({ ...current, [field]: saved.intakeSnapshot?.intake[field] ?? saved.intake[field] })); }}><option value="">仅补充说明，不更改事实</option>{Object.entries(FACT_FIELDS).map(([field, spec]) => <option key={field} value={field}>{spec.label}</option>)}</select></label>}
      {requester ? inputs : null}
      <label>{requester && fields.length ? '补充说明（可选）' : requester ? '补充说明' : '审查操作指引'}<textarea value={answer} disabled={busy} onChange={(e) => setAnswer(e.target.value)} rows={3} placeholder={requester ? '说明具体业务事实或尚不确定的事项' : '这段内容仅作为操作指引，不会成为案件事实'} /></label>
      <div className="case-fact-question__footer"><button type="button" className="btn-primary" disabled={busy || (!fields.length || !requester) && !answer.trim()} onClick={submit}>{busy ? <LoaderCircle size={16} className="case-busy-icon" /> : <ArrowRight size={16} />}{busy ? '正在处理…' : requester && fields.length ? '记录补充事实，供我确认' : '回答并继续'}</button><small>{requester && fields.length ? '下一步：核对内容并明确确认' : requester ? '补充说明不会自动变成已确认事实' : '操作指引不会写入案件事实'}</small></div>
    </>}
    {onEditMaterial && <div className="case-fact-question__materials"><span>需要用材料说明？</span><button type="button" className="btn-link" disabled={busy} onClick={onEditMaterial}><FilePlus2 size={15} aria-hidden="true" />上传或更新材料</button></div>}
  </section>;
}
