import { useEffect, useState, type FormEvent } from 'react';
import { Link } from 'react-router-dom';
import { ApiError, getEvalDraft, getQualityMetrics, listEvalDrafts, saveEvalDraft, transitionEvalDraft } from '../api/client';
import type { DraftError, EvalDraft, EvalDraftSummary, EvalRubric, QualityMetrics } from '../types/evaluation';
import { groupDraftErrors } from '../utils/evalDraftErrors';
import '../styles/operations.css';

const draftLabels = { draft: '草稿', ready: '待导出', exported: '已导出', discarded: '已丢弃' };
const rubricFields = {
  allowed_judgments: '允许的判断', forbidden_judgments: '禁止的判断', must_cover_sources: '必须引用的法源',
  forbidden_clause_sources: '不得作为条款引用的法源', required_exceptions: '需覆盖的例外',
  must_not_assume: '不得假定的事实', clarification_expectations: '应追问的事项',
} satisfies Record<Exclude<keyof EvalRubric, 'should_abstain'>, string>;
type RubricField = keyof typeof rubricFields;
const rubricKeys = Object.keys(rubricFields) as RubricField[];
const fields = ['question', 'material_text', 'intake', 'reviewer_correction', 'selection_reason', 'reference_basis', 'deidentified',
  'rubric', ...rubricKeys.map((key) => `rubric.${key}`), 'rubric.should_abstain'];

export default function EvaluationConsole({ selected }: { selected: string | null }) {
  const [items, setItems] = useState<EvalDraftSummary[]>([]);
  const [metrics, setMetrics] = useState<QualityMetrics | null>(null);
  const [draft, setDraft] = useState<EvalDraft | null>(null);
  const [since, setSince] = useState('');
  const [error, setError] = useState('');
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let active = true;
    void Promise.all([getQualityMetrics(since), listEvalDrafts()]).then(([quality, drafts]) => {
      if (active) { setMetrics(quality); setItems(drafts.items); setError(''); }
    }).catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : '无法加载质量与评测'); });
    return () => { active = false; };
  }, [since, revision]);
  const [draftError, setDraftError] = useState('');
  useEffect(() => {
    if (!selected) return;
    let active = true;
    void getEvalDraft(selected).then((result) => { if (active) { setDraft(result); setDraftError(''); } })
      .catch((reason: unknown) => { if (active) setDraftError(reason instanceof Error ? reason.message : '无法读取评测草稿'); });
    return () => { active = false; };
  }, [selected]);
  const rate = (value: number | null) => value === null ? '暂无数据' : new Intl.NumberFormat('zh-Hans', { style: 'percent', maximumFractionDigits: 1 }).format(value);
  const changed = (row: EvalDraft) => { setDraft(row); setRevision((value) => value + 1); };
  return <div className="ops-page">
    <header className="ops-page__header"><h1 className="page-title">质量与评测</h1>
      <label className="ops-field">统计起始日<input type="date" value={since} onChange={(event) => setSince(event.target.value)} /></label>
    </header>
    {error ? <p className="ops-error" role="alert">{error}</p> : null}
    {metrics ? <div className="ops-detail-grid">
      <section className="ops-panel ops-summary"><h2>审查反馈</h2><dl>
        <div><dt>已复核案件</dt><dd>{metrics.reviewed_cases}</dd></div>
        <div><dt>结论有用率</dt><dd>{rate(metrics.conclusion_useful_rate)}</dd></div>
        <div><dt>引用核查准确率</dt><dd>{rate(metrics.citation_checks.accuracy)}</dd></div>
        <div><dt>人工核查</dt><dd>{metrics.citation_checks.correct} 条正确 · {metrics.citation_checks.wrong} 条错误</dd></div>
      </dl></section>
      <section className="ops-panel ops-summary"><h2>审批与后续工作</h2><dl>
        <div><dt>审批结果</dt><dd>{metrics.approval_outcomes.approved} 通过 · {metrics.approval_outcomes.conditionally_approved} 附条件 · {metrics.approval_outcomes.rejected} 驳回</dd></div>
        <div><dt>评测草稿</dt><dd>{metrics.eval_drafts.draft} 草稿 · {metrics.eval_drafts.ready} 待导出 · {metrics.eval_drafts.exported} 已导出 · {metrics.eval_drafts.discarded} 已丢弃</dd></div>
        <div><dt>法规变更</dt><dd>{metrics.regulation_impact.open} 待处理 · {metrics.regulation_impact.dismissed} 已忽略 · {metrics.regulation_impact.rereview_started} 复审中 · {metrics.regulation_impact.resolved} 已结案</dd></div>
      </dl></section>
    </div> : null}
    {selected ? draft?.id === selected ? <DraftEditor key={`${draft.id}:${draft.status}`} draft={draft} onChanged={changed} />
      : <p className="ops-empty" role="status">{draftError || '正在读取草稿…'}</p> : null}
    <section className="ops-panel ops-history" aria-label="评测草稿">
      <div className="ops-section-title"><h2>评测草稿</h2></div>
      {items.length ? <table className="ops-diff">
        <thead><tr><th scope="col">用例</th><th scope="col">原案</th><th scope="col">状态</th><th scope="col">更新时间</th></tr></thead>
        <tbody>{items.map((row) => <tr key={row.id}>
          <th scope="row"><Link className="ops-link" to={`/governance?tab=quality&draft=${row.id}`} aria-current={selected === row.id ? 'true' : undefined}>
            {row.exported_case_id ?? `草稿 · ${row.id.slice(-8)}`}
          </Link></th>
          <td><Link className="ops-link" to={`/cases/${row.case_id}`}>查看案件</Link></td>
          <td><span className="ops-tag">{draftLabels[row.status]}</span></td>
          <td>{row.updated_at.slice(0, 10)}</td>
        </tr>)}</tbody>
      </table> : <p className="ops-empty ops-empty--compact">{metrics ? '暂无评测草稿' : '正在读取评测草稿…'}</p>}
    </section>
  </div>;
}

function DraftEditor({ draft, onChanged }: { draft: EvalDraft; onChanged: (row: EvalDraft) => void }) {
  const [question, setQuestion] = useState(draft.question);
  const [material, setMaterial] = useState(draft.material_text);
  const [intake, setIntake] = useState(JSON.stringify(draft.intake, null, 2));
  const [correction, setCorrection] = useState(draft.reviewer_correction);
  const [selectionReason, setSelectionReason] = useState(draft.selection_reason);
  const [basis, setBasis] = useState(draft.reference_basis.join('\n'));
  const [rules, setRules] = useState(() => Object.fromEntries(rubricKeys.map((key) =>
    [key, (draft.rubric[key] ?? []).join('\n')])) as Record<RubricField, string>);
  const [abstain, setAbstain] = useState(draft.rubric.should_abstain == null ? '' : String(draft.rubric.should_abstain));
  const [deidentified, setDeidentified] = useState(draft.deidentified);
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<Record<string, DraftError[]>>({});
  const [message, setMessage] = useState('');
  const editable = draft.status === 'draft';
  const availableSources = [...new Set(draft.agent_output.citations.map((citation) => citation.source_id))];
  const splitLines = (value: string) => value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  const editText = (setter: (value: string) => void, value: string) => { setter(value); setDeidentified(false); };
  const errorList = (field: string) => errors[field]?.length ? <ul className="ops-error" role="alert">
    {errors[field].map((error, index) => <li key={index}>{error.field !== field ? `${error.field}：` : ''}{error.message}
      {error.offset !== undefined ? `（位置 ${error.offset}：${error.masked ?? ''}）` : ''}</li>)}
  </ul> : null;

  async function perform(action: 'save' | 'ready' | 'reopen' | 'discard') {
    setBusy(true);
    setErrors({});
    setMessage('');
    try {
      if (action === 'save' || action === 'ready') {
        let parsed: unknown;
        try {
          parsed = JSON.parse(intake);
          if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error();
        } catch {
          setErrors({ intake: [{ field: 'intake', message: '申报信息须为有效的 JSON 对象' }] });
          return;
        }
        const rubric = { ...Object.fromEntries(rubricKeys.map((key) => [key, splitLines(rules[key])])),
          should_abstain: abstain === '' ? null : abstain === 'true' };
        const updated = await saveEvalDraft(draft.id, {
          question, material_text: material, intake: parsed as Record<string, unknown>,
          reviewer_correction: correction, selection_reason: selectionReason, reference_basis: splitLines(basis), rubric, deidentified,
        });
        onChanged(updated);
      }
      if (action !== 'save') onChanged(await transitionEvalDraft(draft.id, action));
      else setMessage('草稿已保存');
    } catch (reason) {
      const issues = reason instanceof ApiError ? (reason.detail as { errors?: DraftError[] } | null)?.errors : null;
      if (issues?.length) setErrors(groupDraftErrors(issues, fields));
      else setMessage(reason instanceof Error ? reason.message : '草稿操作失败，请重试');
    } finally {
      setBusy(false);
    }
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    const button = (event.nativeEvent as SubmitEvent).submitter as HTMLButtonElement | null;
    void perform(button?.value === 'ready' ? 'ready' : 'save');
  }

  return <form className="ops-panel ops-form" onSubmit={submit} aria-label="评测草稿编辑">
    <div className="ops-actions"><h2>评测草稿</h2><span className="ops-tag">{draftLabels[draft.status]}</span>
      <Link className="ops-link" to={`/cases/${draft.case_id}`}>查看原案</Link></div>
    <fieldset className="eval-fields" disabled={!editable || busy}>
      <div className="ops-form-grid">
        <section className="eval-fields">
          <h3>脱敏后的输入</h3>
          <label className="ops-field">问题<textarea rows={2} value={question} onChange={(event) => editText(setQuestion, event.target.value)} /></label>{errorList('question')}
          <label className="ops-field">材料<textarea rows={9} value={material} onChange={(event) => editText(setMaterial, event.target.value)} /></label>{errorList('material_text')}
          <label className="ops-field">申报信息（JSON）<textarea rows={7} value={intake} onChange={(event) => editText(setIntake, event.target.value)} /></label>{errorList('intake')}
        </section>
        <section className="eval-fields">
          <h3>复核对照</h3>
          <div className="ops-impact">
            <strong>{draft.agent_output.legal_path || '原审查结论'}</strong>
            <p>{draft.agent_output.decision_summary}</p>
            <p>{draft.agent_output.conclusion}</p>
            {availableSources.length ? <p className="ops-secondary">{availableSources.join(' · ')}</p> : null}
          </div>
          <label className="ops-field">审查人纠正说明<textarea rows={4} value={correction} onChange={(event) => setCorrection(event.target.value)} /></label>{errorList('reviewer_correction')}
          <label className="ops-field">选题理由<textarea rows={2} value={selectionReason} onChange={(event) => editText(setSelectionReason, event.target.value)} /></label>{errorList('selection_reason')}
          <label className="ops-field">参考依据（每行一项）<textarea rows={3} value={basis} onChange={(event) => editText(setBasis, event.target.value)} /></label>{errorList('reference_basis')}
        </section>
      </div>
      <h3>判断标准</h3>
      {errorList('rubric')}
      <div className="ops-form-grid">
        {rubricKeys.map((key) => <div className="eval-fields" key={key}>
          <label className="ops-field">{rubricFields[key]}（每行一项）<textarea rows={3} value={rules[key]} onChange={(event) => {
            setRules((current) => ({ ...current, [key]: event.target.value })); setDeidentified(false);
          }} /></label>
          {key === 'must_cover_sources' && availableSources.length ? <label className="ops-field">从原引用添加
            <select value="" onChange={(event) => {
              const source = event.target.value;
              if (source) {
                setRules((current) => ({ ...current, must_cover_sources: [...new Set([...splitLines(current.must_cover_sources), source])].join('\n') }));
                setDeidentified(false);
              }
            }}><option value="">选择法源</option>{availableSources.map((source) => <option key={source} value={source}>{source}</option>)}</select>
          </label> : null}
          {errorList(`rubric.${key}`)}
        </div>)}
        <label className="ops-field">证据不足时收口<select value={abstain} onChange={(event) => editText(setAbstain, event.target.value)}>
          <option value="">不限定</option><option value="true">应收口</option><option value="false">不应收口</option>
        </select></label>{errorList('rubric.should_abstain')}
      </div>
      <label><input type="checkbox" checked={deidentified} onChange={(event) => setDeidentified(event.target.checked)} /> 标记为已脱敏</label>
      {errorList('deidentified')}
    </fieldset>
    {errorList('form')}
    {message ? <p className="ops-secondary" role="status">{message}</p> : null}
    <div className="ops-actions">
      {editable ? <><button type="submit" value="save" className="btn-secondary" disabled={busy}>保存草稿</button>
        <button type="submit" value="ready" className="btn-primary" disabled={busy}>校验并标为就绪</button></> : null}
      {draft.status === 'ready' ? <button type="button" className="btn-secondary" disabled={busy} onClick={() => void perform('reopen')}>重新编辑</button> : null}
      {editable || draft.status === 'ready' ? <button type="button" className="btn-secondary" disabled={busy} onClick={() => {
        if (window.confirm('确认丢弃此评测草稿？')) void perform('discard');
      }}>丢弃</button> : null}
    </div>
  </form>;
}
