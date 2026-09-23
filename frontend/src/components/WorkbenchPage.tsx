import { useRef, useState } from 'react';
import type { ChangeEvent, DragEvent } from 'react';
import type { CaseIntake, DashboardSummaryApi } from '../types/api';
import { validateUploadFile } from '../api/client';

interface WorkbenchPageProps {
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
  missingFactKeys: string[];
  historyCount: number;
  summary: DashboardSummaryApi | null;
}

/** Rule-engine fact keys, in the wording a business user answers them. */
const FACT_QUESTIONS: Record<string, string> = {
  important_data: '这批数据里是否包含重要数据？',
  is_ciio: '贵公司是否属于关键信息基础设施运营者？',
  contains_personal_information: '出境的数据是否包含个人信息？',
  contains_sensitive_personal_information: '是否涉及敏感个人信息？',
  cumulative_personal_information_subjects: '当年累计出境的个人信息主体大约有多少人？',
  cumulative_sensitive_personal_information_subjects: '当年累计出境的敏感个人信息主体大约有多少人？',
  exemption_facts_confirmed: '主张的法定豁免情形，构成事实是否已经逐项确认？',
};

const FACT_FIELDS: Record<string, string> = {
  important_data: 'important_data_status',
  is_ciio: 'ciio_status',
  contains_personal_information: 'data_types',
  contains_sensitive_personal_information: 'sensitive_personal_info',
  cumulative_personal_information_subjects: 'annual_non_sensitive_count',
  cumulative_sensitive_personal_information_subjects: 'annual_sensitive_count',
  exemption_facts_confirmed: 'contract_status',
};

const MAX_QUESTIONS = 4;

function updateIntake(intake: CaseIntake, onChange: (next: CaseIntake) => void, key: keyof CaseIntake, value: string | boolean | null | string[]): void {
  onChange({ ...intake, [key]: value });
}

export default function WorkbenchPage({
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
  missingFactKeys,
  historyCount,
  summary,
}: WorkbenchPageProps): JSX.Element {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [fileError, setFileError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [step, setStep] = useState<1 | 2>(1);
  const hasMaterial = Boolean(material.trim() || files.length);
  const canSubmit = !loading && Boolean(question.trim()) && hasMaterial;
  const busy = loading || analyzing;

  const acceptFiles = (incoming: FileList | null): void => {
    if (!incoming || incoming.length === 0) return;
    const accepted: File[] = [];
    for (const file of Array.from(incoming)) {
      try {
        validateUploadFile(file);
        accepted.push(file);
      } catch (errorValue) {
        setFileError(errorValue instanceof Error ? errorValue.message : '文件校验失败');
        return;
      }
    }
    setFileError(null);
    setFiles((current) => [...current, ...accepted.filter((item) => !current.some((existing) => existing.name === item.name && existing.size === item.size))]);
  };

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>): void => {
    acceptFiles(event.target.files);
    event.target.value = '';
  };

  const handleDrop = (event: DragEvent<HTMLDivElement>): void => {
    event.preventDefault();
    setDragging(false);
    acceptFiles(event.dataTransfer?.files ?? null);
  };

  const removeFile = (index: number): void => setFiles((current) => current.filter((_, itemIndex) => itemIndex !== index));

  const goToStepTwo = async (): Promise<void> => {
    if (!canSubmit) return;
    if (await onAnalyze(question.trim(), material.trim(), files)) setStep(2);
  };

  const submit = (): void => {
    if (canSubmit) onSubmit(question.trim(), material.trim(), intake, files);
  };

  const askedFields = missingFactKeys
    .map((key) => FACT_FIELDS[key])
    .filter((field): field is string => Boolean(field))
    .slice(0, MAX_QUESTIONS);
  const askedKeys = new Set(askedFields);

  const fields: Record<string, JSX.Element> = {
    business_activity: <label className="form-field form-field--wide"><span>业务活动</span><input value={intake.business_activity} onChange={(event) => updateIntake(intake, onIntakeChange, 'business_activity', event.target.value)} placeholder="例如：推荐系统、客服平台、人力资源管理" /></label>,
    data_types: <label className="form-field form-field--wide"><span>数据类型</span><input value={intake.data_types.join('、')} onChange={(event) => updateIntake(intake, onIntakeChange, 'data_types', event.target.value.split(/[、,，]/).map((item) => item.trim()).filter(Boolean))} placeholder="手机号、定位信息" /></label>,
    sensitive_personal_info: <label className="form-field"><span>敏感个人信息是否涉及</span><select value={intake.sensitive_personal_info === null ? '' : intake.sensitive_personal_info ? 'yes' : 'no'} onChange={(event) => updateIntake(intake, onIntakeChange, 'sensitive_personal_info', event.target.value === '' ? null : event.target.value === 'yes')}><option value="">尚未确认</option><option value="yes">涉及</option><option value="no">不涉及</option></select></label>,
    overseas_recipient: <label className="form-field"><span>境外接收方</span><input value={intake.overseas_recipient} onChange={(event) => updateIntake(intake, onIntakeChange, 'overseas_recipient', event.target.value)} placeholder="公司/供应商名称" /></label>,
    destination_region: <label className="form-field"><span>目的地</span><input value={intake.destination_region} onChange={(event) => updateIntake(intake, onIntakeChange, 'destination_region', event.target.value)} placeholder="国家或地区" /></label>,
    important_data_status: <label className="form-field"><span>重要数据识别状态</span><select value={intake.important_data_status} onChange={(event) => updateIntake(intake, onIntakeChange, 'important_data_status', event.target.value as CaseIntake['important_data_status'])}><option value="unknown">尚未判断</option><option value="not_important">已确认不涉及</option><option value="important">已确认涉及</option><option value="under_review">正在评估</option></select></label>,
    ciio_status: <label className="form-field"><span>关基运营者状态</span><select value={intake.ciio_status} onChange={(event) => updateIntake(intake, onIntakeChange, 'ciio_status', event.target.value as CaseIntake['ciio_status'])}><option value="unknown">尚未判断</option><option value="not_ciio">已确认不是</option><option value="ciio">已确认是</option><option value="under_review">正在评估</option></select></label>,
    annual_non_sensitive_count: <label className="form-field"><span>非敏感个人信息数量</span><input value={intake.annual_non_sensitive_count} onChange={(event) => updateIntake(intake, onIntakeChange, 'annual_non_sensitive_count', event.target.value)} placeholder="年度估算区间" /></label>,
    annual_sensitive_count: <label className="form-field"><span>敏感个人信息数量</span><input value={intake.annual_sensitive_count} onChange={(event) => updateIntake(intake, onIntakeChange, 'annual_sensitive_count', event.target.value)} placeholder="年度估算区间" /></label>,
    contract_status: <label className="form-field"><span>合同/标准合同状态</span><input value={intake.contract_status} onChange={(event) => updateIntake(intake, onIntakeChange, 'contract_status', event.target.value)} placeholder="未签署、已签署、待法务确认" /></label>,
    transfer_mechanism: <label className="form-field"><span>拟采用的出境路径</span><input value={intake.transfer_mechanism} onChange={(event) => updateIntake(intake, onIntakeChange, 'transfer_mechanism', event.target.value)} placeholder="评估、标准合同、认证或待判断" /></label>,
    processing_purpose: <label className="form-field form-field--wide"><span>处理目的与补充说明</span><textarea value={`${intake.processing_purpose}${intake.notes ? `\n${intake.notes}` : ''}`} onChange={(event) => updateIntake(intake, onIntakeChange, 'processing_purpose', event.target.value)} placeholder="补充业务目的、例外情况和当前已知限制" rows={3} /></label>,
  };
  const otherFields = Object.keys(fields).filter((key) => !askedKeys.has(key));

  return (
    <div className="workbench" aria-busy={busy}>
      <header className="workspace-hero">
        <div>
          <h1 className="page-title">{editingCaseId ? '补充案件信息' : '创建合规案件'}</h1>
        </div>
        <div className="workspace-hero__metric"><strong>{summary?.total_cases ?? historyCount}</strong><span>案件总数</span></div>
        <div className="workspace-hero__risk-strip">
          <span><i className="risk-dot risk-dot--high" />高风险 <strong>{summary?.risk_counts.high ?? 0}</strong></span>
          <span><i className="risk-dot risk-dot--medium" />中风险 <strong>{summary?.risk_counts.medium ?? 0}</strong></span>
          <span><i className="risk-dot risk-dot--insufficient" />待补充 <strong>{summary?.risk_counts.insufficient_evidence ?? 0}</strong></span>
        </div>
      </header>

      <section className="intake-progress card">
        <div className={'intake-progress__step' + (step === 1 ? ' is-active' : ' is-done')}><span>01</span><div><strong>提供材料</strong><small>问题与材料</small></div></div>
        <div className="intake-progress__line" />
        <div className={'intake-progress__step' + (step === 2 ? ' is-active' : '')}><span>02</span><div><strong>补充关键要素</strong><small>只问影响结论的部分</small></div></div>
        <div className="intake-progress__line" />
        <div className="intake-progress__step"><span>03</span><div><strong>提交审查</strong><small>证据与行动</small></div></div>
      </section>

      {error ? <div className="error-box" role="alert"><span className="error-box__mark">!</span><div>{error}</div></div> : null}

      {step === 1 ? (
        <section className="card intake-card">
          <div className="section-heading-row"><div><h2>案件信息</h2></div></div>
          <label className="form-label" htmlFor="wb-question">审查问题</label>
          <input id="wb-question" className="workbench__input" value={question} onChange={(event) => onQuestionChange(event.target.value)} placeholder="例如：这个业务是否需要数据出境安全评估？" disabled={busy} />
          <label className="form-label form-label--material" htmlFor="wb-material">待审查材料</label>
          <div
            className={'material-dropzone' + (dragging ? ' is-dragging' : '')}
            onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={handleDrop}
          >
            <div className="material-toolbar">
              <span>{files.length ? `已选择 ${files.length} 个文件` : '把材料拖到这里，或粘贴项目说明、数据流、供应商信息或合同片段'}</span>
              <input ref={fileInputRef} type="file" accept=".txt,.md,.markdown,.pdf,.docx,.html,.htm,.json,.csv" multiple onChange={handleFileChange} hidden />
              <button type="button" className="btn-secondary" onClick={() => fileInputRef.current?.click()} disabled={busy}>选择文件</button>
            </div>
            {files.length ? <ul className="material-dropzone__files">{files.map((file, index) => <li key={`${file.name}-${index}`}><span>{file.name}</span><button type="button" className="btn-link" onClick={() => removeFile(index)} disabled={busy}>移除</button></li>)}</ul> : null}
          </div>
          {fileError ? <div className="form-error">{fileError}</div> : null}
          <textarea id="wb-material" className="workbench__textarea" value={material} onChange={(event) => onMaterialChange(event.target.value)} placeholder="也可以直接粘贴材料正文；多份材料可以一起拖进来" disabled={busy} rows={10} />
          <div className="intake-card__footer"><button type="button" className="btn-primary" disabled={!canSubmit || busy} onClick={() => void goToStepTwo()}>{analyzing ? 'Agent 正在读取材料…' : '继续，让 Agent 先读一遍材料 →'}</button></div>
        </section>
      ) : (
        <section className="card intake-card">
          <div className="section-heading-row"><div><h2>补充影响结论的要素</h2></div><button type="button" className="btn-link" onClick={() => setStep(1)}>← 返回材料</button></div>
          <p className="intake-hint">
            {askedFields.length
              ? `Agent 已读完材料，还有 ${askedFields.length} 项会影响结论，需要你确认。其余要素已按材料填好，可在下方展开修改。`
              : 'Agent 已从材料中读出全部影响结论的要素。你可以在下方核对并修改，然后提交。'}
          </p>
          {askedFields.map((field) => (
            <div className="intake-question" key={field}>
              <p className="intake-question__title">{FACT_QUESTIONS[missingFactKeys.find((key) => FACT_FIELDS[key] === field) ?? ''] ?? ''}</p>
              <div className="intake-group__grid">{fields[field]}</div>
            </div>
          ))}
          <details className="intake-group intake-group--optional">
            <summary>其他要素（Agent 已识别，可修改）</summary>
            <div className="intake-group__grid">{otherFields.map((key) => fields[key])}</div>
          </details>
          <div className="intake-confirmation"><label><input type="checkbox" checked={intake.cross_border_transfer === true} onChange={(event) => updateIntake(intake, onIntakeChange, 'cross_border_transfer', event.target.checked)} /> <strong>我确认材料涉及向境外提供数据</strong></label><span>未确认事实会标为待补充。</span></div>
          <div className="intake-card__footer"><span>提交后 Agent 会自动开始审查。</span><button type="button" className="btn-primary" disabled={!canSubmit || busy} onClick={submit}>{loading ? '正在提交案件…' : editingCaseId ? '保存补充并重新提交' : '创建案件并提交审查'}</button></div>
        </section>
      )}

    </div>
  );
}
