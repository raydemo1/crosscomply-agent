import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import type { ChangeEvent, DragEvent } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { Paperclip, Upload, X } from 'lucide-react';
import type { CaseIntake } from '../types/api';
import {
  ApiError,
  createCase,
  createCaseFromQA,
  extractIntake,
  freezeMaterialSnapshot,
  listMaterialVersions,
  updateCase,
  updateCaseStatus,
  uploadMaterial,
  validateUploadFile,
} from '../api/client';
import { EMPTY_INTAKE } from '../store/caseStore';
import { allocateUploadNames, PASTED_MATERIAL_LOGICAL_NAME } from '../utils/materialNames';
import '../styles/workbench.css';

interface WorkbenchPageProps {
  /** 通过 ?case= 进入时携带案件详情，页面进入补充/更正模式。 */
  editSeed: {
    id: string;
    question: string;
    materialText: string;
    intake: CaseIntake;
    materialSnapshot: { version_ids: string[] } | null;
  } | null;
  /** 通过路由 state 传入的预填内容（问答带入 / 模板 / 以此重审）。 */
  seed: {
    handoff?: {
      conversationId: string;
      runId: string;
      requestId: string;
      question: string;
      leads?: string;
      files?: File[];
      materialIds: string[];
    };
    template?: { question: string; intake: CaseIntake };
    rerun?: { question: string; material: string };
  } | null;
}

type MissingFact = { key: string; reason: string; input_type: 'choice' | 'text' | 'count' | 'exemption' | 'free_text' };

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
const MATERIAL_FALLBACK_PREFIX = '材料以附件形式提供：';

function materialOriginal(material: string): File {
  return new File([material], 'case-material.txt', { type: 'text/plain;charset=utf-8' });
}

/** 粘贴正文的编辑视为对该份材料的更新，而不是新上传。 */
function replacesPastedMaterial(text: string, originalText: string): boolean {
  return Boolean(text.trim()) && text !== originalText;
}

function materialFallback(files: File[]): string {
  return files.length ? `${MATERIAL_FALLBACK_PREFIX}${files.map((file) => file.name).join('、')}` : '';
}

/** 仅文件上传的案件把文件名摘要存进材料正文；编辑时不要把它当成真实材料。 */
function editableMaterialText(materialText: string): string {
  return materialText.startsWith(MATERIAL_FALLBACK_PREFIX) ? '' : materialText;
}

/**
 * 冻结版本只能来自本案已冻结的版本：读取全部历史版本会悄悄复活
 * 已被替换的草稿与已删除的附件。
 */
async function loadFrozenVersions(caseId: string, keptVersionIds: string[]) {
  if (!keptVersionIds.length) return [];
  const kept = new Set(keptVersionIds);
  const keptVersions = (await listMaterialVersions(caseId))
    .filter((version) => kept.has(version.id))
    .map((version) => ({ id: version.id, logicalName: version.logical_name, versionNumber: version.version_number }));
  if (keptVersions.length !== kept.size) {
    throw new ApiError(0, '本案已有材料发生变化，请刷新页面后重新提交。', '/api/cases');
  }
  return keptVersions;
}

/** 快照对每个逻辑材料至多保留一个版本，更高版本直接替换。 */
function frozenVersionIds(versions: Array<{ id: string; logicalName: string; versionNumber: number }>): string[] {
  const newest = new Map<string, { id: string; logicalName: string; versionNumber: number }>();
  for (const version of versions) {
    const current = newest.get(version.logicalName);
    if (!current || version.versionNumber > current.versionNumber) {
      newest.set(version.logicalName, version);
    }
  }
  return [...newest.values()].map((version) => version.id);
}

/** 三态答案把「尚不确定」与明确的「否」区分开。 */
function triStateValue(value: boolean | null): string {
  return value === null ? '' : value ? 'yes' : 'no';
}

function triStateUpdate(value: string): boolean | null {
  return value === '' ? null : value === 'yes';
}

function updateIntake(intake: CaseIntake, onChange: (next: CaseIntake) => void, key: keyof CaseIntake, value: string | boolean | null | string[]): void {
  onChange({ ...intake, [key]: value });
}

export default function WorkbenchPage({ editSeed, seed }: WorkbenchPageProps): JSX.Element {
  const navigate = useNavigate();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const dragDepthRef = useRef(0);
  const [question, setQuestion] = useState('');
  const [material, setMaterial] = useState('');
  const [intake, setIntake] = useState<CaseIntake>({ ...EMPTY_INTAKE });
  const [files, setFiles] = useState<File[]>([]);
  const [fileError, setFileError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [step, setStep] = useState<1 | 2>(1);
  const [missingFacts, setMissingFacts] = useState<MissingFact[]>([]);
  const [loading, setLoading] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [investigationLeads, setInvestigationLeads] = useState('');
  const [editingCaseId, setEditingCaseId] = useState<string | null>(null);
  const [editingVersionIds, setEditingVersionIds] = useState<string[]>([]);
  const [editingMaterialText, setEditingMaterialText] = useState('');
  const [existingMaterialNames, setExistingMaterialNames] = useState<string[]>([]);

  const handoff = seed?.handoff ?? null;

  // 预填：问答带入 / 模板 / 以此重审（每次进入页面只应用一次）
  const seedAppliedRef = useRef(false);
  useEffect(() => {
    if (seedAppliedRef.current || !seed) return;
    seedAppliedRef.current = true;
    if (seed.handoff) {
      setQuestion(seed.handoff.question);
      setFiles(seed.handoff.files ?? []);
      setInvestigationLeads(seed.handoff.leads ?? '');
    } else if (seed.template) {
      setQuestion(seed.template.question);
      setIntake({ ...EMPTY_INTAKE, ...seed.template.intake, data_types: [...(seed.template.intake.data_types ?? [])] });
    } else if (seed.rerun) {
      setQuestion(seed.rerun.question);
      setMaterial(editableMaterialText(seed.rerun.material));
    }
  }, [seed]);

  // 编辑模式：从案件详情带出的案件填充表单
  useEffect(() => {
    if (!editSeed) return;
    setEditingCaseId(editSeed.id);
    setQuestion(editSeed.question);
    const editable = editableMaterialText(editSeed.materialText);
    setMaterial(editable);
    setIntake({ ...editSeed.intake, data_types: [...editSeed.intake.data_types] });
    setEditingVersionIds(editSeed.materialSnapshot?.version_ids ?? []);
    setEditingMaterialText(editable);
    setMissingFacts([]);
    setStep(1);
  }, [editSeed]);

  // 新上传文件的编号依赖已冻结的材料名，进入编辑模式时先取一次
  useEffect(() => {
    if (!editingCaseId || !editingVersionIds.length) { setExistingMaterialNames([]); return; }
    let cancelled = false;
    void listMaterialVersions(editingCaseId).then((versions) => {
      if (cancelled) return;
      const kept = new Set(editingVersionIds);
      setExistingMaterialNames(versions.filter((version) => kept.has(version.id)).map((version) => version.logical_name));
    }).catch(() => { if (!cancelled) setExistingMaterialNames([]); });
    return () => { cancelled = true; };
  }, [editingCaseId, editingVersionIds]);

  const hasMaterial = Boolean(material.trim() || files.length || existingMaterialNames.length);
  const canSubmit = !loading && Boolean(question.trim()) && hasMaterial;
  const busy = loading || analyzing;
  const hasNewMaterialToRead = Boolean(material.trim() || files.length);

  // 同一份提交路径使用的编号分配，页面展示的名称即之后冻结的名称
  const uploadNames = useMemo(
    () => allocateUploadNames(files.map((file) => file.name), existingMaterialNames, replacesPastedMaterial(material, editingMaterialText)),
    [files, existingMaterialNames, editingMaterialText, material],
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
    // 同名文件全部保留，提交时再消除歧义；静默丢材料对证据而言不可接受
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

  const handleDragEnter = (): void => {
    dragDepthRef.current += 1;
    setDragging(true);
  };

  const handleDragLeave = (): void => {
    dragDepthRef.current = Math.max(0, dragDepthRef.current - 1);
    if (dragDepthRef.current === 0) setDragging(false);
  };

  const removeFile = (index: number): void => setFiles((current) => current.filter((_, itemIndex) => itemIndex !== index));

  const handleAnalyze = async (): Promise<boolean> => {
    setAnalyzing(true);
    setError(null);
    try {
      const result = await extractIntake(question.trim(), material.trim(), files);
      setIntake((current) => ({ ...current, ...result.intake, data_types: [...result.intake.data_types] }));
      setMissingFacts(result.missing);
      return true;
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : reason instanceof Error ? reason.message : 'Agent 无法读取材料，请检查文件后重试。');
      return false;
    } finally {
      setAnalyzing(false);
    }
  };

  const goToStepTwo = async (): Promise<void> => {
    if (!canSubmit) return;
    // 仅编辑事实时没有新材料可读，无需再让 Agent 读一遍
    if (!hasNewMaterialToRead) {
      setStep(2);
      return;
    }
    if (await handleAnalyze()) setStep(2);
  };

  const handleSubmit = async (): Promise<void> => {
    setLoading(true);
    setError(null);
    const textMaterial = material.trim();
    const keptVersionIds = editingCaseId ? editingVersionIds : [];
    const updatesPasted = replacesPastedMaterial(textMaterial, editingMaterialText);
    const clearEditing = (): void => {
      setEditingCaseId(null);
      setEditingVersionIds([]);
      setEditingMaterialText('');
    };
    try {
      if (!keptVersionIds.length && !files.length && !updatesPasted) {
        throw new ApiError(0, '请提供至少一份待审查材料。', '/api/cases');
      }
      const originFiles = handoff?.files ?? [];
      const selectedOriginIds = handoff ? handoff.materialIds.filter((_, index) => files.includes(originFiles[index])) : [];
      const saved = editingCaseId
        ? await updateCase(editingCaseId, {
          question: question.trim(),
          ...(updatesPasted ? { material_text: textMaterial } : {}),
          intake,
        })
        : handoff
          ? await createCaseFromQA(handoff.conversationId, {
            question: question.trim(),
            material_text: textMaterial || materialFallback(files),
            intake,
            run_id: handoff.runId,
            request_id: handoff.requestId,
            material_ids: selectedOriginIds,
          })
          : await createCase({ question: question.trim(), materialText: textMaterial || materialFallback(files), intake });
      const frozenVersions = await loadFrozenVersions(saved.case.id, keptVersionIds);
      const importedVersions = 'imported_versions' in saved ? saved.imported_versions as import('../types/api').MaterialVersionApi[] : [];
      frozenVersions.push(...importedVersions.map((version) => ({ id: version.id, logicalName: version.logical_name, versionNumber: version.version_number })));
      const newFiles = files.filter((file) => !originFiles.includes(file));
      const fileNames = allocateUploadNames(
        newFiles.map((file) => file.name),
        frozenVersions.map((version) => version.logicalName),
        updatesPasted,
      );
      const uploads = newFiles.map((file, index) => ({ logicalName: fileNames[index], file }));
      if (updatesPasted) uploads.push({ logicalName: PASTED_MATERIAL_LOGICAL_NAME, file: materialOriginal(textMaterial) });
      const uploaded: Array<{ id: string; logicalName: string; versionNumber: number }> = [];
      for (const upload of uploads) {
        const created = await uploadMaterial(saved.case.id, upload.logicalName, upload.file);
        uploaded.push({ id: created.id, logicalName: created.logical_name, versionNumber: created.version_number });
      }
      await freezeMaterialSnapshot(saved.case.id, frozenVersionIds([...frozenVersions, ...uploaded]));
      const pending = await updateCaseStatus(saved.case.id, 'pending_review');
      clearEditing();
      navigate(`/cases/${pending.case.id}`);
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : reason instanceof Error ? reason.message : '提交案件时发生未知错误');
    } finally {
      setLoading(false);
    }
  };

  // 多个事实可能指向同一问题（如敏感/非敏感人数共享统计口径），按问题文本去重
  const seenQuestions = new Set<string>();
  // 人数口径已随两处人数问题内联出现，不再单独提问
  const countPeriodShared = missingFacts.some(
    (item) => item.key !== 'count_period' && (FACT_FIELDS[item.key] ?? []).includes('count_period'),
  );
  const uniqueFacts = missingFacts.filter((fact) => {
    const questionText = FACT_QUESTIONS[fact.key] ?? fact.reason;
    if (seenQuestions.has(questionText)) return false;
    seenQuestions.add(questionText);
    if (fact.key === 'count_period' && countPeriodShared) return false;
    return true;
  });
  const askedFacts = uniqueFacts.slice(0, MAX_QUESTIONS);
  const requestedFields = [...new Set(missingFacts.flatMap((item) => FACT_FIELDS[item.key] ?? []))];
  const askedKeys = new Set(requestedFields);

  const renderFact = (fact: MissingFact): JSX.Element => {
    const fieldsForFact = FACT_FIELDS[fact.key] ?? [];
    const value = intake.followup_answers?.[fact.key] ?? '';
    const updateAnswer = (answer: string): void => setIntake({
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
    business_activity: <label className="form-field form-field--wide"><span>业务活动</span><input value={intake.business_activity} onChange={(event) => updateIntake(intake, setIntake, 'business_activity', event.target.value)} placeholder="例如：推荐系统、客服平台、人力资源管理" /></label>,
    data_types: <label className="form-field form-field--wide"><span>数据类型（业务描述）</span><input value={intake.data_types.join('、')} onChange={(event) => updateIntake(intake, setIntake, 'data_types', event.target.value.split(/[、,，]/).map((item) => item.trim()).filter(Boolean))} placeholder="例如：手机号、定位信息" /></label>,
    cross_border_transfer: <label className="form-field"><span>是否向境外提供数据</span><select value={triStateValue(intake.cross_border_transfer)} onChange={(event) => updateIntake(intake, setIntake, 'cross_border_transfer', triStateUpdate(event.target.value))}><option value="">尚不确定</option><option value="yes">是，涉及出境</option><option value="no">否，不涉及出境</option></select></label>,
    contains_personal_information: <label className="form-field"><span>出境数据是否包含个人信息</span><select value={triStateValue(intake.contains_personal_information)} onChange={(event) => updateIntake(intake, setIntake, 'contains_personal_information', triStateUpdate(event.target.value))}><option value="">尚不确定</option><option value="yes">是，包含个人信息</option><option value="no">否，不含个人信息</option></select></label>,
    sensitive_personal_info: <label className="form-field"><span>是否涉及敏感个人信息</span><select value={triStateValue(intake.sensitive_personal_info)} onChange={(event) => updateIntake(intake, setIntake, 'sensitive_personal_info', triStateUpdate(event.target.value))}><option value="">尚不确定</option><option value="yes">涉及</option><option value="no">不涉及</option></select></label>,
    overseas_recipient: <label className="form-field"><span>境外接收方</span><input value={intake.overseas_recipient} onChange={(event) => updateIntake(intake, setIntake, 'overseas_recipient', event.target.value)} placeholder="例如：境外供应商、集团关联公司" /></label>,
    destination_region: <label className="form-field"><span>目的地</span><input value={intake.destination_region} onChange={(event) => updateIntake(intake, setIntake, 'destination_region', event.target.value)} placeholder="例如：新加坡" /></label>,
    legal_basis_or_consent: <label className="form-field form-field--wide"><span>法律依据或同意安排</span><textarea value={intake.legal_basis_or_consent} onChange={(event) => updateIntake(intake, setIntake, 'legal_basis_or_consent', event.target.value)} placeholder="例如：已取得单独同意，或说明处理所依据的具体业务和法律基础" rows={3} /></label>,
    important_data_status: <label className="form-field"><span>重要数据识别状态</span><select value={intake.important_data_status} onChange={(event) => updateIntake(intake, setIntake, 'important_data_status', event.target.value as CaseIntake['important_data_status'])}><option value="unknown">尚不确定</option><option value="not_important">已确认不涉及</option><option value="important">已确认涉及</option><option value="under_review">正在核实</option></select></label>,
    ciio_status: <label className="form-field"><span>关基运营者状态</span><select value={intake.ciio_status} onChange={(event) => updateIntake(intake, setIntake, 'ciio_status', event.target.value as CaseIntake['ciio_status'])}><option value="unknown">尚不确定</option><option value="not_ciio">已确认不是</option><option value="ciio">已确认是</option><option value="under_review">正在核实</option></select></label>,
    annual_non_sensitive_count: <label className="form-field"><span>非敏感个人信息数量</span><input value={intake.annual_non_sensitive_count} onChange={(event) => updateIntake(intake, setIntake, 'annual_non_sensitive_count', event.target.value)} placeholder="例如：1 万–5 万人" /></label>,
    annual_sensitive_count: <label className="form-field"><span>敏感个人信息数量</span><input value={intake.annual_sensitive_count} onChange={(event) => updateIntake(intake, setIntake, 'annual_sensitive_count', event.target.value)} placeholder="例如：1000–5000 人" /></label>,
    count_period: <label className="form-field"><span>人数统计口径</span><select value={intake.count_period} onChange={(event) => updateIntake(intake, setIntake, 'count_period', event.target.value as CaseIntake['count_period'])}><option value="unknown">尚不确定</option><option value="current_year_cumulative">本年度 1 月 1 日起累计</option><option value="annual_estimate">全年估算</option><option value="other">其他口径</option></select></label>,
    contract_status: <label className="form-field"><span>合同/标准合同状态</span><input value={intake.contract_status} onChange={(event) => updateIntake(intake, setIntake, 'contract_status', event.target.value)} placeholder="例如：已签署、待法务确认" /></label>,
    transfer_mechanism: <label className="form-field"><span>拟采用的出境路径</span><input value={intake.transfer_mechanism} onChange={(event) => updateIntake(intake, setIntake, 'transfer_mechanism', event.target.value)} placeholder="例如：标准合同、安全评估、认证" /></label>,
    processing_purpose: <label className="form-field form-field--wide"><span>处理目的</span><textarea value={intake.processing_purpose} onChange={(event) => updateIntake(intake, setIntake, 'processing_purpose', event.target.value)} placeholder="例如：客服响应、订单履约或安全运维" rows={3} /></label>,
    exemption_facts: <label className="form-field form-field--wide"><span>主张豁免的事实依据</span><textarea value={intake.exemption_facts} onChange={(event) => updateIntake(intake, setIntake, 'exemption_facts', event.target.value)} placeholder="例如：与个人履行合同的具体关系，或其他主张豁免的业务事实" rows={3} /></label>,
    notes: <label className="form-field form-field--wide"><span>补充说明</span><textarea value={intake.notes} onChange={(event) => updateIntake(intake, setIntake, 'notes', event.target.value)} placeholder="补充当前已知限制或 Agent 无法从材料确认的事实" rows={3} /></label>,
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
          <input id="wb-question" className="workbench__input" value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="例如：这个业务是否需要数据出境安全评估？" disabled={busy} />
          <label className="form-label form-label--material" htmlFor="wb-material">审查材料</label>
          <div
            className={'material-dropzone' + (dragging ? ' is-dragging' : '')}
            onDragEnter={handleDragEnter}
            onDragLeave={handleDragLeave}
            onDragOver={(event) => event.preventDefault()}
            onDrop={handleDrop}
          >
            <textarea id="wb-material" className="workbench__textarea" value={material} onChange={(event) => setMaterial(event.target.value)} placeholder="也可以直接粘贴材料正文，多份材料可一起拖入" disabled={busy} rows={10} />
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
          <div className="intake-card__footer"><span className="intake-card__hint">{!canSubmit && !busy ? '请先填写审查问题并提供材料' : ''}</span><button type="button" className="btn-primary" disabled={!canSubmit || busy} onClick={handleSubmit}>{loading ? '正在提交案件…' : editingCaseId ? '保存补充并重新提交' : '提交审查'}</button></div>
        </section>
      )}

    </div>
  );
}
