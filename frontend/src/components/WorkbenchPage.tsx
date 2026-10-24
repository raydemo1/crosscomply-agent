import { Fragment, useMemo, useRef, useState } from 'react';
import type { ChangeEvent, DragEvent } from 'react';
import { Paperclip, Upload, X } from 'lucide-react';
import type { CaseIntake } from '../types/api';
import { validateUploadFile } from '../api/client';
import { allocateUploadNames } from '../utils/materialNames';
import '../styles/workbench.css';

interface WorkbenchPageProps {
  initialFiles?: File[];
  investigationLeads?: string;
  question: string;
  material: string;
  intake: CaseIntake;
  editingCaseId: string | null;
  onQuestionChange: (value: string) => void;
  onMaterialChange: (value: string) => void;
  onIntakeChange: (value: CaseIntake) => void;
  /** Reads the material with the Agent and fills what it can before the user answers anything. */
  onAnalyze: (question: string, material: string, files: File[]) => Promise<boolean>;
  onSubmit: (question: string, material: string, intake: CaseIntake, files: File[]) => void;
  loading: boolean;
  analyzing: boolean;
  error: string | null;
  /** Facts the Agent could not read from the material and that change the conclusion. */
  missingFacts: Array<{ key: string; reason: string; input_type: 'choice' | 'text' | 'count' | 'exemption' | 'free_text' }>;
  /** Names already frozen on the case being edited; new uploads are numbered to avoid them. */
  existingMaterialNames: string[];
  /** Whether the pasted prose is being re-uploaded, which reserves its stable name first. */
  reservePastedMaterial: boolean;
}

const FACT_QUESTIONS: Record<string, string> = {
  important_data_status: '这批数据是否已被确认为重要数据？',
  ciio_status: '公司是否已被认定为关键信息基础设施运营者？',
  annual_non_sensitive_count: '统计期间内，非敏感个人信息出境涉及多少人？',
  annual_sensitive_count: '统计期间内，敏感个人信息出境涉及多少人？',
  count_period: '人数按哪个期间和口径统计？',
  destination_region: '数据将传输至哪个国家或地区？',
  exemption_facts: '可能适用豁免的具体业务事实是什么？',
  sensitive_personal_info: '是否涉及敏感个人信息？',
  cross_border_transfer: '这项活动是否涉及向境外提供数据？',
  important_data: '这批数据里是否包含重要数据？',
  is_ciio: '公司是否属于关键信息基础设施运营者？',
  contains_personal_information: '出境的数据是否包含个人信息？',
  contains_sensitive_personal_information: '是否涉及敏感个人信息？',
  cumulative_personal_information_subjects: '当年累计出境的个人信息主体大约有多少人？',
  cumulative_sensitive_personal_information_subjects: '当年累计出境的敏感个人信息主体大约有多少人？',
  exemption_facts_confirmed: '主张的法定豁免情形，构成事实是否已经逐项确认？',
  overseas_recipient: '境外接收方是谁？',
  processing_purpose: '数据出境具体用于什么业务目的？',
  legal_basis_or_consent: '处理个人信息的法律依据或同意安排是什么？',
  data_volume_threshold: '请确认当年累计出境人数及统计口径。',
};

const FACT_FIELDS: Record<string, string[]> = {
  important_data_status: ['important_data_status'],
  ciio_status: ['ciio_status'],
  annual_non_sensitive_count: ['annual_non_sensitive_count', 'count_period'],
  annual_sensitive_count: ['annual_sensitive_count', 'count_period'],
  count_period: ['count_period'],
  destination_region: ['destination_region'],
  exemption_facts: ['exemption_facts'],
  cross_border_transfer: ['cross_border_transfer'],
  important_data: ['important_data_status'],
  is_ciio: ['ciio_status'],
  contains_personal_information: ['contains_personal_information'],
  sensitive_personal_info: ['sensitive_personal_info'],
  contains_sensitive_personal_information: ['sensitive_personal_info'],
  cumulative_personal_information_subjects: ['annual_non_sensitive_count', 'count_period'],
  cumulative_sensitive_personal_information_subjects: ['annual_sensitive_count', 'count_period'],
  data_volume_threshold: ['annual_non_sensitive_count', 'annual_sensitive_count', 'count_period'],
  overseas_recipient: ['overseas_recipient'],
  processing_purpose: ['processing_purpose'],
  legal_basis_or_consent: ['legal_basis_or_consent'],
  exemption_facts_confirmed: ['exemption_facts'],
};

const MAX_QUESTIONS = 4;

/** Tri-state answers keep "尚不确定" distinct from a confirmed "否". */
function triStateValue(value: boolean | null): string {
  return value === null ? '' : value ? 'yes' : 'no';
}

function triStateUpdate(value: string): boolean | null {
  return value === '' ? null : value === 'yes';
}

function updateIntake(intake: CaseIntake, onChange: (next: CaseIntake) => void, key: keyof CaseIntake, value: string | boolean | null | string[]): void {
  onChange({ ...intake, [key]: value });
}

export default function WorkbenchPage({
  initialFiles = [],
  investigationLeads,
  question,
  material,
  intake,
  editingCaseId,
  onQuestionChange,
  onMaterialChange,
  onIntakeChange,
  onAnalyze,
  onSubmit,
  loading,
  analyzing,
  error,
  missingFacts,
  existingMaterialNames,
  reservePastedMaterial,
}: WorkbenchPageProps): JSX.Element {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dragDepthRef = useRef(0);
  const [files, setFiles] = useState<File[]>(initialFiles);
  const [fileError, setFileError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [step, setStep] = useState<1 | 2>(1);
  const hasMaterial = Boolean(material.trim() || files.length || existingMaterialNames.length);
  const canSubmit = !loading && Boolean(question.trim()) && hasMaterial;
  const busy = loading || analyzing;

  // The same allocation the submit path uses, so the names shown here are the names frozen later.
  const uploadNames = useMemo(
    () => allocateUploadNames(files.map((file) => file.name), existingMaterialNames, reservePastedMaterial),
    [files, existingMaterialNames, reservePastedMaterial],
  );

  const acceptFiles = (incoming: FileList | null): void => {
    if (!incoming || incoming.length === 0) return;
    const accepted: File[] = [];
    for (const file of Array.from(incoming)) {
      try {
        validateUploadFile(file);
        accepted.push(file);
      } catch (errorValue) {
        setFileError(errorValue instanceof Error ? errorValue.message : '文件校验失败，请更换文件后重试。');
        return;
      }
    }
    setFileError(null);
    // Every file the user picked is kept, even when two share a name. Same-named uploads are
    // disambiguated on submit; silently dropping material is not acceptable for evidence.
    setFiles((current) => [...current, ...accepted]);
  };

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>): void => {
    acceptFiles(event.target.files);
    event.target.value = '';
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>): void => {
    event.preventDefault();
    dragDepthRef.current = 0;
    setDragging(false);
    acceptFiles(event.dataTransfer?.files ?? null);
  };

  // 进入/离开按层计数：拖过 textarea 等子元素时提示不闪烁
  const handleDragEnter = (): void => {
    dragDepthRef.current += 1;
    setDragging(true);
  };

  const handleDragLeave = (): void => {
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setDragging(false);
  };

  const removeFile = (index: number): void => setFiles((current) => current.filter((_, itemIndex) => itemIndex !== index));

  const hasNewMaterialToRead = Boolean(material.trim() || files.length);

  const goToStepTwo = async (): Promise<void> => {
    if (!canSubmit) return;
    // Editing only the facts of an existing case adds no material to read. Asking the
    // Agent to read again here would demand a re-upload that the case does not need.
    if (!hasNewMaterialToRead) {
      setStep(2);
      return;
    }
    if (await onAnalyze(question.trim(), material.trim(), files)) setStep(2);
  };

  const submit = (): void => {
    if (canSubmit) onSubmit(question.trim(), material.trim(), intake, files);
  };

  // 多个事实可能指向同一问题（如敏感/非敏感人数共享统计口径），按问题文本去重
  const seenQuestions = new Set<string>();
  const uniqueFacts = missingFacts.filter((fact) => {
    const question = FACT_QUESTIONS[fact.key] ?? fact.reason;
    if (seenQuestions.has(question)) return false;
    seenQuestions.add(question);
    return true;
  });
  const askedFacts = uniqueFacts.slice(0, MAX_QUESTIONS);
  const requestedFields = [...new Set(missingFacts.flatMap((item) => FACT_FIELDS[item.key] ?? []))];
  const askedKeys = new Set(requestedFields);

  const renderFact = (fact: WorkbenchPageProps['missingFacts'][number]): JSX.Element => {
    const fieldsForFact = FACT_FIELDS[fact.key] ?? [];
    const value = intake.followup_answers?.[fact.key] ?? '';
    const updateAnswer = (answer: string): void => onIntakeChange({
      ...intake,
      followup_answers: { ...intake.followup_answers, [fact.key]: answer },
    });
    return <div className="intake-question" key={fact.key}>
      <p className="intake-question__title">{FACT_QUESTIONS[fact.key] ?? fact.reason}</p>
      <div className="intake-group__grid">
        {fieldsForFact.length ? fieldsForFact.map((field) => <Fragment key={field}>{fields[field]}</Fragment>) : (
          <label className="form-field form-field--wide"><span>你的回答</span>
            {fact.input_type === 'choice' ? <select aria-label={fact.reason} value={value} onChange={(event) => updateAnswer(event.target.value)}><option value="">尚不确定</option><option value="yes">是</option><option value="no">否</option><option value="unknown">无法确认</option></select>
              : fact.input_type === 'count' ? <input aria-label={fact.reason} type="number" min="0" value={value} onChange={(event) => updateAnswer(event.target.value)} placeholder="例如：500" />
                : <textarea aria-label={fact.reason} value={value} onChange={(event) => updateAnswer(event.target.value)} rows={3} placeholder="请针对这项问题单独说明" />}
          </label>
        )}
      </div>
    </div>;
  };

  const fields: Record<string, JSX.Element> = {
    business_activity: <label className="form-field form-field--wide"><span>业务活动</span><input value={intake.business_activity} onChange={(event) => updateIntake(intake, onIntakeChange, 'business_activity', event.target.value)} placeholder="例如：推荐系统、客服平台、人力资源管理" /></label>,
    data_types: <label className="form-field form-field--wide"><span>数据类型（业务描述）</span><input value={intake.data_types.join('、')} onChange={(event) => updateIntake(intake, onIntakeChange, 'data_types', event.target.value.split(/[、,，]/).map((item) => item.trim()).filter(Boolean))} placeholder="例如：手机号、定位信息" /></label>,
    cross_border_transfer: <label className="form-field"><span>是否向境外提供数据</span><select value={triStateValue(intake.cross_border_transfer)} onChange={(event) => updateIntake(intake, onIntakeChange, 'cross_border_transfer', triStateUpdate(event.target.value))}><option value="">尚不确定</option><option value="yes">是，涉及出境</option><option value="no">否，不涉及出境</option></select></label>,
    contains_personal_information: <label className="form-field"><span>出境数据是否包含个人信息</span><select value={triStateValue(intake.contains_personal_information)} onChange={(event) => updateIntake(intake, onIntakeChange, 'contains_personal_information', triStateUpdate(event.target.value))}><option value="">尚不确定</option><option value="yes">是，包含个人信息</option><option value="no">否，不含个人信息</option></select></label>,
    sensitive_personal_info: <label className="form-field"><span>是否涉及敏感个人信息</span><select value={triStateValue(intake.sensitive_personal_info)} onChange={(event) => updateIntake(intake, onIntakeChange, 'sensitive_personal_info', triStateUpdate(event.target.value))}><option value="">尚不确定</option><option value="yes">涉及</option><option value="no">不涉及</option></select></label>,
    overseas_recipient: <label className="form-field"><span>境外接收方</span><input value={intake.overseas_recipient} onChange={(event) => updateIntake(intake, onIntakeChange, 'overseas_recipient', event.target.value)} placeholder="例如：境外供应商、集团关联公司" /></label>,
    destination_region: <label className="form-field"><span>目的地</span><input value={intake.destination_region} onChange={(event) => updateIntake(intake, onIntakeChange, 'destination_region', event.target.value)} placeholder="例如：新加坡" /></label>,
    legal_basis_or_consent: <label className="form-field form-field--wide"><span>法律依据或同意安排</span><textarea value={intake.legal_basis_or_consent} onChange={(event) => updateIntake(intake, onIntakeChange, 'legal_basis_or_consent', event.target.value)} placeholder="例如：已取得单独同意，或说明处理所依据的具体业务和法律基础" rows={3} /></label>,
    important_data_status: <label className="form-field"><span>重要数据识别状态</span><select value={intake.important_data_status} onChange={(event) => updateIntake(intake, onIntakeChange, 'important_data_status', event.target.value as CaseIntake['important_data_status'])}><option value="unknown">尚不确定</option><option value="not_important">已确认不涉及</option><option value="important">已确认涉及</option><option value="under_review">正在核实</option></select></label>,
    ciio_status: <label className="form-field"><span>关基运营者状态</span><select value={intake.ciio_status} onChange={(event) => updateIntake(intake, onIntakeChange, 'ciio_status', event.target.value as CaseIntake['ciio_status'])}><option value="unknown">尚不确定</option><option value="not_ciio">已确认不是</option><option value="ciio">已确认是</option><option value="under_review">正在核实</option></select></label>,
    annual_non_sensitive_count: <label className="form-field"><span>非敏感个人信息数量</span><input value={intake.annual_non_sensitive_count} onChange={(event) => updateIntake(intake, onIntakeChange, 'annual_non_sensitive_count', event.target.value)} placeholder="例如：1 万–5 万人" /></label>,
    annual_sensitive_count: <label className="form-field"><span>敏感个人信息数量</span><input value={intake.annual_sensitive_count} onChange={(event) => updateIntake(intake, onIntakeChange, 'annual_sensitive_count', event.target.value)} placeholder="例如：1000–5000 人" /></label>,
    count_period: <label className="form-field"><span>人数统计口径</span><select value={intake.count_period} onChange={(event) => updateIntake(intake, onIntakeChange, 'count_period', event.target.value as CaseIntake['count_period'])}><option value="unknown">尚不确定</option><option value="current_year_cumulative">本年度 1 月 1 日起累计</option><option value="annual_estimate">全年估算</option><option value="other">其他口径</option></select></label>,
    contract_status: <label className="form-field"><span>合同/标准合同状态</span><input value={intake.contract_status} onChange={(event) => updateIntake(intake, onIntakeChange, 'contract_status', event.target.value)} placeholder="例如：已签署、待法务确认" /></label>,
    transfer_mechanism: <label className="form-field"><span>拟采用的出境路径</span><input value={intake.transfer_mechanism} onChange={(event) => updateIntake(intake, onIntakeChange, 'transfer_mechanism', event.target.value)} placeholder="例如：标准合同、安全评估、认证" /></label>,
    processing_purpose: <label className="form-field form-field--wide"><span>处理目的</span><textarea value={intake.processing_purpose} onChange={(event) => updateIntake(intake, onIntakeChange, 'processing_purpose', event.target.value)} placeholder="例如：客服响应、订单履约或安全运维" rows={3} /></label>,
    exemption_facts: <label className="form-field form-field--wide"><span>主张豁免的事实依据</span><textarea value={intake.exemption_facts} onChange={(event) => updateIntake(intake, onIntakeChange, 'exemption_facts', event.target.value)} placeholder="例如：与个人履行合同的具体关系，或其他主张豁免的业务事实" rows={3} /></label>,
    notes: <label className="form-field form-field--wide"><span>补充说明</span><textarea value={intake.notes} onChange={(event) => updateIntake(intake, onIntakeChange, 'notes', event.target.value)} placeholder="补充当前已知限制或 Agent 无法从材料确认的事实" rows={3} /></label>,
  };
  const otherFields = Object.keys(fields).filter((key) => !askedKeys.has(key));

  return (
    <div className="workbench" aria-busy={busy}>
      <header className="page-header">
        <div className="page-header__copy">
          <h1 className="page-title">{editingCaseId ? '补充案件信息' : '发起合规审查'}</h1>
          <p className="page-header__lede">{editingCaseId ? '补充或更正案件信息后，将重新提交审查。' : '填写审查问题并上传材料，系统将依据适用法律出具审查报告。'}</p>
        </div>
      </header>

      {investigationLeads ? <details className="qa-workbench-leads"><summary>来自合规问答的调查线索</summary><p>以下内容仅供参考，不作为已确认事实或正式审查结论。</p><div>{investigationLeads}</div></details> : null}

      <section className="intake-progress card" aria-label="审查进度">
        <div className={'intake-progress__step' + (step === 1 ? ' is-active' : ' is-done')}><span>{step === 1 ? '01' : '✓'}</span><div><strong>提供材料</strong><small>描述问题，上传或粘贴材料</small></div></div>
        <div className="intake-progress__line" />
        <div className={'intake-progress__step' + (step === 2 ? ' is-active' : '')}><span>02</span><div><strong>补充关键要素</strong><small>只核对影响结论的信息</small></div></div>
      </section>

      {error ? <div className="error-box" role="alert"><span className="error-box__mark">!</span><div>{error}</div></div> : null}

      {step === 1 ? (
        <section className="card intake-card">
          <div className="section-heading-row"><h2>案件信息</h2></div>
          <label className="form-label" htmlFor="wb-question">审查问题</label>
          <input id="wb-question" className="workbench__input" value={question} onChange={(event) => onQuestionChange(event.target.value)} placeholder="例如：这个业务是否需要数据出境安全评估？" disabled={busy} />
          <label className="form-label form-label--material" htmlFor="wb-material">审查材料</label>
          <div
            className={'material-dropzone' + (dragging ? ' is-dragging' : '')}
            onDragEnter={handleDragEnter}
            onDragLeave={handleDragLeave}
            onDragOver={(event) => event.preventDefault()}
            onDrop={handleDrop}
          >
            <textarea id="wb-material" className="workbench__textarea" value={material} onChange={(event) => onMaterialChange(event.target.value)} placeholder="也可以直接粘贴材料正文，多份材料可一起拖入" disabled={busy} rows={10} />
            {dragging ? <div className="material-dropzone__overlay" aria-hidden="true"><Upload size={22} strokeWidth={1.8} /><span>松开以添加材料文件</span></div> : null}
            <div className="material-row">
              <input ref={fileInputRef} type="file" accept=".txt,.md,.markdown,.pdf,.docx,.html,.htm,.json,.csv" multiple onChange={handleFileChange} hidden />
              <button type="button" className="material-attach" title="添加材料文件" aria-label="添加材料文件" disabled={busy} onClick={() => fileInputRef.current?.click()}><Paperclip size={16} strokeWidth={1.8} /></button>
              {files.length ? <ul className="material-chips">{files.map((file, index) => <li key={`${file.name}-${index}`}><span>{uploadNames[index]}</span><button type="button" aria-label={`移除 ${file.name}`} onClick={() => removeFile(index)} disabled={busy}><X size={13} /></button></li>)}</ul> : null}
            </div>
          </div>
          {fileError ? <div className="form-error">{fileError}</div> : null}
          {existingMaterialNames.length ? <p className="intake-hint">本案已有 {existingMaterialNames.length} 份材料，将原样保留在下次审查中；此处新增的文件只会追加，不会替换它们。</p> : null}
          <div className="intake-card__footer"><span className="intake-card__hint">{!canSubmit && !busy ? '请先填写审查问题并提供材料' : ''}</span><button type="button" className="btn-primary" disabled={!canSubmit || busy} onClick={() => void goToStepTwo()}>{analyzing ? 'Agent 正在读取材料…' : hasNewMaterialToRead ? '读取材料并继续' : '继续补充关键信息'}</button></div>
        </section>
      ) : (
        <section className="card intake-card">
          <div className="section-heading-row"><h2>确认关键信息</h2><button type="button" className="btn-link" onClick={() => setStep(1)}>← 返回材料</button></div>
          <p className="intake-hint">
            {missingFacts.length
              ? `Agent 已读完材料，还有 ${uniqueFacts.length} 项会影响结论，需要你确认；未确认的内容可以留空。`
              : '未从材料中识别到会影响结论的缺口；可核对下方案件要素，未确认的内容可以留空。'}
          </p>
          {askedFacts.map(renderFact)}
          {uniqueFacts.length > MAX_QUESTIONS ? <details className="intake-group--optional"><summary>还有 {uniqueFacts.length - MAX_QUESTIONS} 项待确认</summary>{uniqueFacts.slice(MAX_QUESTIONS).map((fact) => {
            return renderFact(fact);
          })}</details> : null}
          <details className="intake-group--optional" open={askedFacts.length === 0}>
            <summary>{askedFacts.length ? '查看并核对其他案件要素' : '核对案件要素'}</summary>
            <div className="intake-group__grid">{otherFields.map((key) => <Fragment key={key}>{fields[key]}</Fragment>)}</div>
          </details>
          <div className="intake-card__footer"><button type="button" className="btn-primary" disabled={!canSubmit || busy} onClick={submit}>{loading ? '正在提交案件…' : editingCaseId ? '保存补充并重新提交' : '提交审查'}</button></div>
        </section>
      )}

    </div>
  );
}
