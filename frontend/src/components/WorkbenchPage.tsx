import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import type { ChangeEvent, DragEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft, ArrowRight, Check, ChevronDown, FileText, Paperclip, X } from 'lucide-react';
import type { CaseIntake, CaseTemplateApi } from '../types/api';
import {
  ApiError,
  ALLOWED_UPLOAD_SUFFIXES,
  MAX_UPLOAD_BYTES,
  createCase,
  createCaseFromQA,
  extractIntake,
  freezeMaterialSnapshot,
  listMaterialVersions,
  listCaseTemplates,
  updateCase,
  updateCaseStatus,
  uploadMaterial,
  validateUploadFile,
} from '../api/client';
import { EMPTY_INTAKE } from '../store/caseStore';
import { allocateUploadNames, pastedAttachment, PASTED_MATERIAL_LOGICAL_NAME } from '../utils/materialNames';
import { intakeQuestion, intakeQuestionFields, visibleIntakeQuestions, type MissingFact } from '../utils/intakeQuestions';
import '../styles/workbench.css';

interface WorkbenchPageProps {
  /** 通过 ?case= 进入时携带案件详情，页面进入补充/更正模式。 */
  editSeed: {
    id: string;
    transferActivityId?: string | null;
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
  const questionInputRef = useRef<HTMLTextAreaElement>(null);
  const pageHeadingRef = useRef<HTMLHeadingElement>(null);
  const reviewHeadingRef = useRef<HTMLHeadingElement>(null);
  const previousStep = useRef<1 | 2>(1);
  const dragDepthRef = useRef(0);
  const [question, setQuestion] = useState('');
  const [material, setMaterial] = useState('');
  const [intake, setIntake] = useState<CaseIntake>({ ...EMPTY_INTAKE });
  const [dataTypesDraft, setDataTypesDraft] = useState<string | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [fileError, setFileError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const [step, setStep] = useState<1 | 2>(1);
  const [extracted, setExtracted] = useState<CaseIntake | null>(null);
  const [templates, setTemplates] = useState<CaseTemplateApi[]>([]);
  const [templatesOpen, setTemplatesOpen] = useState(false);
  const [templatesLoading, setTemplatesLoading] = useState(false);
  const [templatesError, setTemplatesError] = useState<string | null>(null);
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
  const transferReview = Boolean(editSeed?.transferActivityId && !editSeed.materialSnapshot);

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

  const busy = loading || analyzing;
  const hasQuestion = Boolean(question.trim());
  const hasNewMaterialToRead = Boolean(material.trim() || files.length);
  const hasMaterial = hasNewMaterialToRead || existingMaterialNames.length > 0;
  const canSubmit = hasQuestion && hasMaterial;
  const intakeHint = hasQuestion
    ? (hasMaterial ? '' : '请填写业务背景或上传附件。')
    : (hasMaterial ? '请填写审查问题。' : '请填写审查问题，并提供业务背景或附件。');

  useEffect(() => {
    if (previousStep.current === step) return;
    previousStep.current = step;
    (step === 1 ? pageHeadingRef : reviewHeadingRef).current?.focus();
  }, [step]);

  // 同一份提交路径使用的编号分配，页面展示的名称即之后冻结的名称
  const uploadNames = useMemo(
    () => allocateUploadNames(files.map((file) => file.name), existingMaterialNames, replacesPastedMaterial(material, editingMaterialText)),
    [files, existingMaterialNames, editingMaterialText, material],
  );

  const acceptFiles = (incoming: ArrayLike<File> | null): void => {
    if (busy || !incoming || incoming.length === 0) return;
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
    if (!event.dataTransfer.types.includes('Files')) return;
    event.preventDefault();
    dragDepthRef.current = 0;
    setDragging(false);
    acceptFiles(event.dataTransfer?.files ?? null);
  };

  const handleDragEnter = (event: DragEvent<HTMLDivElement>): void => {
    if (busy || !event.dataTransfer.types.includes('Files')) return;
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
      const extractedValues = transferReview
        ? Object.fromEntries(Object.entries(result.intake).filter(([, value]) =>
          value !== '' && value !== null && value !== 'unknown' && (!Array.isArray(value) || value.length > 0),
        ))
        : result.intake;
      const parsed = { ...intake, ...extractedValues };
      setIntake({ ...parsed, business_entity: intake.business_entity });
      setExtracted(structuredClone(parsed));
      setMissingFacts(result.missing);
      return true;
    } catch (reason) {
      setError(reason instanceof ApiError ? reason.message : reason instanceof Error ? reason.message : 'CrossComply 无法读取材料，请检查文件后重试。');
      return false;
    } finally {
      setAnalyzing(false);
    }
  };

  const goToStepTwo = async (): Promise<void> => {
    if (!canSubmit || busy) return;
    setTemplatesOpen(false);
    // 仅编辑事实时没有新材料可读，无需再让 Agent 读一遍
    if (!hasNewMaterialToRead) {
      setStep(2);
      return;
    }
    if (await handleAnalyze()) setStep(2);
  };

  const openTemplates = async (): Promise<void> => {
    if (templatesOpen) { setTemplatesOpen(false); return; }
    setTemplatesOpen(true); setTemplatesLoading(true); setTemplatesError(null);
    try { setTemplates((await listCaseTemplates()).items.filter((item) => !item.archived)); }
    catch (reason) { setTemplatesError(reason instanceof Error ? reason.message : '无法读取模板'); }
    finally { setTemplatesLoading(false); }
  };
  const applyTemplate = (template: CaseTemplateApi): void => {
    if (question.trim() && !window.confirm('使用模板将替换审查问题和案件要素，已添加的材料会保留。是否继续？')) return;
    setQuestion(template.question); setIntake({ ...EMPTY_INTAKE, ...template.intake });
    setExtracted(null); setMissingFacts([]); setTemplatesOpen(false);
    questionInputRef.current?.focus();
  };

  const handleSubmit = async (): Promise<void> => {
    if (!canSubmit || busy) return;
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

  const uniqueFacts = visibleIntakeQuestions(missingFacts, Infinity);
  const MAX_QUESTIONS = 4;
  const askedFacts = uniqueFacts.slice(0, MAX_QUESTIONS);
  const requestedFields = [...new Set(missingFacts.flatMap((item) => intakeQuestionFields(item.key)))];
  const renderedFields = new Set<string>();
  const askedKeys = new Set<string>(requestedFields);

  const renderFact = (fact: MissingFact): JSX.Element => {
    const fieldsForFact = intakeQuestionFields(fact.key).filter((field) => {
      if (renderedFields.has(field)) return false;
      renderedFields.add(field);
      return true;
    });
    const value = intake.followup_answers?.[fact.key] ?? '';
    const updateAnswer = (answer: string): void => setIntake({
      ...intake,
      followup_answers: { ...intake.followup_answers, [fact.key]: answer },
    });
    return <div className="intake-question" key={fact.key}>
      {!fieldsForFact.length ? <p className="intake-question__title">{intakeQuestion(fact.key, intake.count_period, fact.reason)}</p> : null}
      <div className="intake-group__grid">
        {fieldsForFact.length ? fieldsForFact.map((field) => <Fragment key={field}>{fields[field]}</Fragment>) : (
          <label className="form-field form-field--wide"><span>你的回答</span>
            {fact.input_type === 'choice' ? <select aria-label={intakeQuestion(fact.key, intake.count_period, fact.reason)} value={value} onChange={(event) => updateAnswer(event.target.value)}><option value="">尚不确定</option><option value="yes">是</option><option value="no">否</option><option value="unknown">无法确认</option></select>
              : fact.input_type === 'count' ? <input aria-label={intakeQuestion(fact.key, intake.count_period, fact.reason)} type="number" min="0" value={value} onChange={(event) => updateAnswer(event.target.value)} placeholder="暂不确定可留空" />
                : <textarea aria-label={intakeQuestion(fact.key, intake.count_period, fact.reason)} value={value} onChange={(event) => updateAnswer(event.target.value)} rows={3} placeholder="请针对这项问题单独说明" />}
          </label>
        )}
      </div>
    </div>;
  };

  const fields: Record<string, JSX.Element> = {
    business_entity: <label className="form-field form-field--wide"><span>申报主体（法人全称）</span><input maxLength={160} value={intake.business_entity} readOnly={Boolean(transferReview)} onChange={(event) => updateIntake(intake, setIntake, 'business_entity', event.target.value)} /></label>,
    business_activity: <label className="form-field form-field--wide"><span>业务活动</span><input value={intake.business_activity} onChange={(event) => updateIntake(intake, setIntake, 'business_activity', event.target.value)} placeholder="例如：推荐系统、客服平台、人力资源管理" /></label>,
    data_types: <label className="form-field form-field--wide"><span>数据类型（业务描述）</span><input value={dataTypesDraft ?? intake.data_types.join('、')} onChange={(event) => {
      const text = event.target.value;
      setDataTypesDraft(text);
      updateIntake(intake, setIntake, 'data_types', text.split(/[、,，]/).map((item) => item.trim()).filter(Boolean));
    }} onBlur={() => setDataTypesDraft(null)} placeholder="例如：手机号、定位信息" /></label>,
    cross_border_transfer: <label className="form-field"><span>是否向境外提供数据</span><select value={triStateValue(intake.cross_border_transfer)} onChange={(event) => updateIntake(intake, setIntake, 'cross_border_transfer', triStateUpdate(event.target.value))}><option value="">尚不确定</option><option value="yes">是，涉及出境</option><option value="no">否，不涉及出境</option></select></label>,
    contains_personal_information: <label className="form-field"><span>出境数据是否包含个人信息</span><select value={triStateValue(intake.contains_personal_information)} onChange={(event) => updateIntake(intake, setIntake, 'contains_personal_information', triStateUpdate(event.target.value))}><option value="">尚不确定</option><option value="yes">是，包含个人信息</option><option value="no">否，不含个人信息</option></select></label>,
    sensitive_personal_info: <label className="form-field"><span>是否涉及敏感个人信息</span><select value={triStateValue(intake.sensitive_personal_info)} onChange={(event) => updateIntake(intake, setIntake, 'sensitive_personal_info', triStateUpdate(event.target.value))}><option value="">尚不确定</option><option value="yes">涉及</option><option value="no">不涉及</option></select></label>,
    overseas_recipient: <label className="form-field"><span>境外接收方</span><input value={intake.overseas_recipient} onChange={(event) => updateIntake(intake, setIntake, 'overseas_recipient', event.target.value)} placeholder="例如：境外供应商、集团关联公司" /></label>,
    ...(transferReview ? { vendor_name: <label className="form-field"><span>供应商</span><input value={intake.vendor_name} onChange={(event) => updateIntake(intake, setIntake, 'vendor_name', event.target.value)} /></label> } : {}),
    destination_region: <label className="form-field"><span>境外接收地</span><input value={intake.destination_region} onChange={(event) => updateIntake(intake, setIntake, 'destination_region', event.target.value)} placeholder="例如：新加坡" /></label>,
    legal_basis_or_consent: <label className="form-field form-field--wide"><span>告知、同意及其他处理依据</span><textarea value={intake.legal_basis_or_consent} onChange={(event) => updateIntake(intake, setIntake, 'legal_basis_or_consent', event.target.value)} placeholder="请说明已履行的手续或所依据的其他事由" rows={3} /></label>,
    important_data_status: <label className="form-field"><span>重要数据识别状态</span><select value={intake.important_data_status} onChange={(event) => updateIntake(intake, setIntake, 'important_data_status', event.target.value as CaseIntake['important_data_status'])}><option value="unknown">尚不确定</option><option value="not_important">已确认不涉及</option><option value="important">已确认涉及</option><option value="under_review">正在核实</option></select></label>,
    ciio_status: <label className="form-field"><span>关键信息基础设施运营者身份</span><select value={intake.ciio_status} onChange={(event) => updateIntake(intake, setIntake, 'ciio_status', event.target.value as CaseIntake['ciio_status'])}><option value="unknown">尚不确定</option><option value="not_ciio">已确认不是</option><option value="ciio">已确认是</option><option value="under_review">正在核实</option></select></label>,
    annual_non_sensitive_count: <label className="form-field"><span>非敏感个人信息涉及人数</span><input value={intake.annual_non_sensitive_count} onChange={(event) => updateIntake(intake, setIntake, 'annual_non_sensitive_count', event.target.value)} placeholder="暂不确定可留空" /></label>,
    annual_sensitive_count: <label className="form-field"><span>敏感个人信息涉及人数</span><input value={intake.annual_sensitive_count} onChange={(event) => updateIntake(intake, setIntake, 'annual_sensitive_count', event.target.value)} placeholder="暂不确定可留空" /></label>,
    count_period: <label className="form-field"><span>人数统计口径</span><select value={intake.count_period} onChange={(event) => updateIntake(intake, setIntake, 'count_period', event.target.value as CaseIntake['count_period'])}><option value="unknown">尚不确定</option><option value="current_year_cumulative">本年度 1 月 1 日起累计</option><option value="annual_estimate">全年估算</option><option value="other">其他口径</option></select></label>,
    contract_status: <label className="form-field"><span>合同/标准合同状态</span><input value={intake.contract_status} onChange={(event) => updateIntake(intake, setIntake, 'contract_status', event.target.value)} placeholder="例如：已签署、待法务确认" /></label>,
    transfer_mechanism: <label className="form-field"><span>拟采用的出境路径</span><input value={intake.transfer_mechanism} onChange={(event) => updateIntake(intake, setIntake, 'transfer_mechanism', event.target.value)} placeholder="例如：标准合同、安全评估、认证" /></label>,
    processing_purpose: <label className="form-field form-field--wide"><span>数据出境用途</span><textarea value={intake.processing_purpose} onChange={(event) => updateIntake(intake, setIntake, 'processing_purpose', event.target.value)} placeholder="例如：客服响应、订单履约或安全运维" rows={3} /></label>,
    exemption_facts: <label className="form-field form-field--wide"><span>相关业务情况</span><textarea value={intake.exemption_facts} onChange={(event) => updateIntake(intake, setIntake, 'exemption_facts', event.target.value)} placeholder="请说明具体业务安排" rows={3} /></label>,
    notes: <label className="form-field form-field--wide"><span>补充说明</span><textarea value={intake.notes} onChange={(event) => updateIntake(intake, setIntake, 'notes', event.target.value)} placeholder="补充材料中未体现的业务情况" rows={3} /></label>,
  };
  for (const field of Object.keys(fields) as Array<keyof CaseIntake>) {
    const original = fields[field];
    const value = intake[field];
    const missing = askedKeys.has(field) && (value == null || value === '' || value === 'unknown' || (Array.isArray(value) && !value.length));
    const edited = field !== 'business_entity' && extracted && JSON.stringify(value) !== JSON.stringify(extracted[field]);
    fields[field] = <div className={'intake-field' + (missing ? ' is-unconfirmed' : '')} key={field}>{original}{edited ? <button type="button" className="btn-link intake-field-restore" onClick={() => setIntake((current) => ({ ...current, [field]: structuredClone(extracted![field]) }))}>恢复提取值</button> : null}</div>;
  }
  const otherFields = Object.keys(fields).filter((key) => key !== 'business_entity' && !askedKeys.has(key));

  return (
    <div className="workbench">
      <header className="workbench__heading">
        <div className="page-header__copy">
          <h1 ref={pageHeadingRef} className="page-title" tabIndex={-1}>{transferReview ? '申报出境变更' : editingCaseId ? '补充案件信息' : '新建案件'}</h1>
          {editingCaseId ? <p className="page-header__lede">{transferReview ? '请上传本次材料，并逐项核对从原审批带入的业务事实。' : '补充或更正后，将重新提交审查。'}</p> : null}
        </div>
        <ol role="list" className="intake-progress" aria-label="提交案件进度">
          <li className="intake-progress__step" aria-current={step === 1 ? 'step' : undefined}>
            <span aria-hidden="true">{step === 1 ? '01' : <Check size={15} />}</span>
            <strong>提供问题与材料</strong>{step === 2 ? <span className="sr-only">（已完成）</span> : null}
          </li>
          <li className="intake-progress__step" aria-current={step === 2 ? 'step' : undefined}>
            <span aria-hidden="true">02</span><strong>核对案件要素</strong>
          </li>
        </ol>
      </header>

      {investigationLeads ? <details className="qa-workbench-leads"><summary>前期问答记录</summary><div>{investigationLeads}</div></details> : null}

      <div className="intake-card">
        {step === 1 ? <>
          <div className="intake-question-heading">
            <label className="form-label" htmlFor="wb-question">审查问题</label>
            {!editingCaseId ? <button type="button" className="btn-link intake-inline-action" disabled={busy} aria-expanded={templatesOpen} onClick={() => void openTemplates()}>
              使用模板 <ChevronDown size={15} aria-hidden="true" />
            </button> : null}
          </div>
          {templatesOpen ? <div className="intake-template-picker">
            {templatesLoading ? <p role="status">正在读取模板…</p>
              : templatesError ? <p className="form-error" role="alert">{templatesError}</p>
                : templates.length ? templates.map((template) => <button key={template.id} type="button" disabled={busy} onClick={() => applyTemplate(template)}>{template.name}</button>)
                  : <p>暂无可用模板</p>}
          </div> : null}
          <textarea ref={questionInputRef} id="wb-question" className="intake-textarea" value={question} onChange={(event) => setQuestion(event.target.value)} placeholder="本业务需要做数据出境安全评估吗？" disabled={busy} aria-required="true" rows={2} />

          <div className="intake-materials" onDragEnter={handleDragEnter} onDragLeave={handleDragLeave} onDragOver={(event) => {
            if (event.dataTransfer.types.includes('Files')) event.preventDefault();
          }} onDrop={handleDrop}>
            <label className="form-label" htmlFor="wb-material">业务背景</label>
            <textarea id="wb-material" className="intake-textarea" value={material} onChange={(event) => setMaterial(event.target.value)} onPaste={(event) => { const file = pastedAttachment(event.clipboardData.getData('text/plain')); if (file) { event.preventDefault(); acceptFiles([file]); } }} placeholder="描述业务安排，或粘贴待审查材料" disabled={busy} rows={5} />
            <div className={'intake-file-drop' + (dragging ? ' is-dragging' : '')}>
              <button type="button" className="intake-file-drop__button" disabled={busy} aria-describedby="intake-file-hint" onClick={() => fileInputRef.current?.click()}>
                <Paperclip size={18} aria-hidden="true" />{dragging ? '松开以添加附件' : '上传或拖入附件'}
              </button>
              <span id="intake-file-hint">PDF、DOCX、TXT 等 · 单文件 {MAX_UPLOAD_BYTES / (1024 * 1024)} MB 以内</span>
            </div>
            <input ref={fileInputRef} type="file" accept={ALLOWED_UPLOAD_SUFFIXES.join(',')} multiple onChange={handleFileChange} hidden />
            {files.length ? <ul className="intake-files" aria-label="已添加附件">
              {files.map((file, index) => <li key={`${file.name}-${index}`}>
                <FileText size={16} aria-hidden="true" /><span>{uploadNames[index]}</span>
                <button type="button" aria-label={`移除 ${uploadNames[index]}`} onClick={() => removeFile(index)} disabled={busy}><X size={17} aria-hidden="true" /></button>
              </li>)}
            </ul> : null}
            <span className="sr-only" role="status">已添加 {files.length} 份附件</span>
            {fileError ? <p className="form-error" role="alert">{fileError}</p> : null}
            {existingMaterialNames.length ? <p className="intake-hint">本案已有 {existingMaterialNames.length} 份材料。新增附件将追加。</p> : null}
          </div>
        </> : <>
          <div className="intake-review-heading">
            <h2 id="intake-review-heading" ref={reviewHeadingRef} tabIndex={-1}>核对案件要素</h2>
            <button type="button" className="btn-link intake-inline-action" disabled={busy} onClick={() => { setError(null); setStep(1); }}><ArrowLeft size={16} aria-hidden="true" />返回材料</button>
          </div>
          <fieldset className="intake-review-fields" disabled={busy} aria-labelledby="intake-review-heading">
            <div className="intake-group__grid">{fields.business_entity}</div>
            {askedFacts.length ? <div className="intake-review-priority">{askedFacts.map(renderFact)}</div> : null}
            {uniqueFacts.length > MAX_QUESTIONS ? <details className="intake-group--optional"><summary><ChevronDown size={16} aria-hidden="true" />还有 {uniqueFacts.length - MAX_QUESTIONS} 项待确认</summary>{uniqueFacts.slice(MAX_QUESTIONS).map(renderFact)}</details> : null}
            {askedFacts.length ? <details className="intake-group--optional"><summary><ChevronDown size={16} aria-hidden="true" />查看其他案件要素</summary>
              <div className="intake-group__grid">{otherFields.map((key) => fields[key])}</div>
            </details> : <div className="intake-group__grid">{otherFields.map((key) => fields[key])}</div>}
          </fieldset>
        </>}
        {error ? <p className="form-error intake-action-error" role="alert">{error}</p> : null}
        <div className="intake-card__footer">
          {step === 1 && intakeHint ? <span id="intake-action-hint" className="intake-hint">{intakeHint}</span> : null}
          <span className="sr-only" role="status">{analyzing ? '正在解析材料…' : loading ? '正在提交案件…' : ''}</span>
          {step === 1 ? <button type="button" className="btn-primary" disabled={!canSubmit} aria-disabled={busy || undefined} aria-describedby={intakeHint ? 'intake-action-hint' : undefined} onClick={() => void goToStepTwo()}>
            {analyzing ? '正在解析材料…' : editingCaseId && !hasNewMaterialToRead ? '核对案件要素' : '解析材料并继续'}
            {!busy ? <ArrowRight size={16} aria-hidden="true" /> : null}
          </button> : <button type="button" className="btn-primary" disabled={!canSubmit} aria-disabled={busy || undefined} onClick={() => void handleSubmit()}>
            {loading ? '正在提交案件…' : editingCaseId ? '保存补充并重新提交' : '提交审查'}
          </button>}
        </div>
      </div>
    </div>
  );
}
