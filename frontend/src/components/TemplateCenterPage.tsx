import { useEffect, useMemo, useRef, useState } from 'react';
import type { ChangeEvent } from 'react';
import { Archive, ArrowRight, Download, FileJson, Pencil, Plus, Search, Upload, X } from 'lucide-react';
import {
  archiveCaseTemplate,
  createCaseTemplate,
  listCaseTemplates,
  updateCaseTemplate,
  ApiError,
} from '../api/client';
import type { CaseIntake, CaseTemplateApi, CaseTemplatePayload, WorkbenchUser } from '../types/api';
import './TemplateCenterPage.css';

const EMPTY_INTAKE: CaseIntake = {
  business_activity: '', data_types: [], contains_personal_information: null, sensitive_personal_info: null, cross_border_transfer: null,
  important_data_status: 'unknown', ciio_status: 'unknown', annual_non_sensitive_count: '',
  count_period: 'unknown',
  annual_sensitive_count: '', overseas_recipient: '', destination_region: '', processing_purpose: '',
  transfer_mechanism: '', vendor_name: '', contract_status: '', legal_basis_or_consent: '', exemption_facts: '', followup_answers: {}, notes: '',
};

const TEMPLATE_FORMAT = 'crosscomply.case-template';

export interface TemplateCenterPageProps {
  onUseTemplate?: (template: CaseTemplateApi) => void;
  /** 模板的增删改（新建、编辑、导入、导出、归档）属于管理员工作，只有 admin 可见。 */
  user?: WorkbenchUser;
}

interface TemplateDraft {
  name: string;
  description: string;
  question: string;
  intake: CaseIntake;
}

const EMPTY_DRAFT: TemplateDraft = {
  name: '', description: '', question: '', intake: { ...EMPTY_INTAKE },
};

function toDraft(template?: CaseTemplateApi | null): TemplateDraft {
  if (!template) return { ...EMPTY_DRAFT, intake: { ...EMPTY_INTAKE } };
  return {
    name: template.name,
    description: template.description,
    question: template.question,
    intake: { ...EMPTY_INTAKE, ...template.intake },
  };
}

function formatDate(value: string): string {
  try {
    return new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(value));
  } catch {
    return value;
  }
}

function asExportPayload(template: CaseTemplateApi): Record<string, unknown> {
  return {
    format: TEMPLATE_FORMAT,
    version: 1,
    name: template.name,
    description: template.description,
    question: template.question,
    intake: template.intake,
    rerank_mode: template.rerank_mode,
  };
}

export default function TemplateCenterPage({ onUseTemplate, user }: TemplateCenterPageProps): JSX.Element {
  const canManageTemplates = user?.role === 'admin';
  const [templates, setTemplates] = useState<CaseTemplateApi[]>([]);
  const [query, setQuery] = useState('');
  const [editing, setEditing] = useState<CaseTemplateApi | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [draft, setDraft] = useState<TemplateDraft>(toDraft());
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [serviceUnavailable, setServiceUnavailable] = useState(false);
  const importRef = useRef<HTMLInputElement>(null);
  const editorRef = useRef<HTMLDialogElement>(null);
  const archiveDialogRef = useRef<HTMLDialogElement>(null);
  const [archiveTarget, setArchiveTarget] = useState<CaseTemplateApi | null>(null);

  // 编辑器是原生 dialog：showModal 提供顶层渲染、Esc 关闭与焦点圈定
  useEffect(() => {
    const dialog = editorRef.current;
    if (!dialog) return;
    if (editorOpen) {
      if (!dialog.open) dialog.showModal();
    } else if (dialog.open) {
      dialog.close();
    }
  }, [editorOpen]);

  useEffect(() => {
    const dialog = archiveDialogRef.current;
    if (!dialog) return;
    if (archiveTarget) {
      if (!dialog.open) dialog.showModal();
    } else if (dialog.open) {
      dialog.close();
    }
  }, [archiveTarget]);

  const load = async (): Promise<void> => {
    const result = await listCaseTemplates(query);
    setTemplates(result.items);
    setServiceUnavailable(false);
  };

  useEffect(() => {
    void load().catch((reason) => {
      setServiceUnavailable(true);
      setMessage(
        reason instanceof ApiError && reason.status === 404
            ? '模板服务暂不可用，请稍后重试或联系管理员。'
            : reason instanceof Error ? reason.message : '无法加载模板',
      );
    });
  }, [query]);

  const visibleTemplates = useMemo(() => templates.filter((item) => !item.archived), [templates]);

  const updateDraft = <K extends keyof TemplateDraft>(key: K, value: TemplateDraft[K]): void => {
    setDraft((current) => ({ ...current, [key]: value }));
  };

  const openCreate = (): void => {
    setEditing(null);
    setDraft(toDraft());
    setEditorOpen(true);
    setMessage(null);
  };

  const openEdit = (template: CaseTemplateApi): void => {
    setEditing(template);
    setDraft(toDraft(template));
    setEditorOpen(true);
    setMessage(null);
  };

  const closeEditor = (): void => {
    if (busy) return;
    setEditing(null);
    setDraft(toDraft());
    setEditorOpen(false);
  };

  const save = async (): Promise<void> => {
    if (!draft.name.trim() || !draft.question.trim()) {
      setMessage('请填写模板名称和审查问题。');
      return;
    }
    setBusy(true);
    setMessage(null);
    try {
      const payload: CaseTemplatePayload = {
        name: draft.name.trim(), description: draft.description.trim(), question: draft.question.trim(),
        intake: draft.intake,
      };
      if (editing) await updateCaseTemplate(editing.id, payload);
      else await createCaseTemplate(payload);
      await load();
      closeEditor();
      setMessage(editing ? '模板已更新。' : '模板已创建。');
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '保存模板失败');
    } finally {
      setBusy(false);
    }
  };

  const archive = async (template: CaseTemplateApi): Promise<void> => {
    setBusy(true);
    try {
      await archiveCaseTemplate(template.id);
      await load();
      setMessage('模板已归档。');
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '归档模板失败');
    } finally {
      setBusy(false);
      setArchiveTarget(null);
    }
  };

  const exportTemplate = (template: CaseTemplateApi): void => {
    const blob = new Blob([JSON.stringify(asExportPayload(template), null, 2)], { type: 'application/json;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = `${template.name.replace(/[\\/:*?"<>|]/g, '_')}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  const importTemplate = async (event: ChangeEvent<HTMLInputElement>): Promise<void> => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    try {
      const raw: unknown = JSON.parse(await file.text());
      if (!raw || typeof raw !== 'object' || Array.isArray(raw)) throw new Error('文件格式有误，请使用 JSON 模板。');
      const value = raw as Partial<TemplateDraft> & { format?: unknown; version?: unknown };
      if (value.format !== TEMPLATE_FORMAT || value.version !== 1) throw new Error('不是有效的 CrossComply 场景模板文件。');
      if (typeof value.name !== 'string' || !value.name.trim() || typeof value.question !== 'string' || !value.question.trim()) {
        throw new Error('导入文件缺少模板名称或审查问题。');
      }
      const payload: CaseTemplatePayload = {
        name: `${value.name.trim()}（导入副本）`, description: typeof value.description === 'string' ? value.description : '',
        question: value.question.trim(), intake: { ...EMPTY_INTAKE, ...(value.intake && typeof value.intake === 'object' ? value.intake : {}) },
      };
      setBusy(true);
      await createCaseTemplate(payload);
      await load();
      setMessage('模板已导入，并创建为副本。');
    } catch (reason) {
      setMessage(reason instanceof SyntaxError ? '文件格式有误，请使用 JSON 模板。' : reason instanceof Error ? reason.message : '导入模板失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="template-page" aria-labelledby="template-page-title">
      <header className="template-page__header">
        <div>
          <h1 id="template-page-title" className="page-title">场景模板</h1>
        </div>
        {canManageTemplates ? (
          <div className="template-page__actions">
            <input ref={importRef} type="file" accept="application/json,.json" hidden onChange={(event) => void importTemplate(event)} />
            <button className="btn-secondary" type="button" onClick={() => importRef.current?.click()} disabled={busy || serviceUnavailable}><Upload size={16} />导入模板</button>
            <button className="btn-primary" type="button" onClick={openCreate} disabled={serviceUnavailable}><Plus size={17} />新建模板</button>
          </div>
        ) : null}
      </header>

      {message && <div className="template-page__message" role="status">{message}</div>}

      <div className="template-toolbar" aria-label="搜索模板">
        <div className="template-search"><Search size={17} strokeWidth={1.8} aria-hidden="true" /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称、适用场景或审查问题" aria-label="搜索模板" /></div>
        <span>{visibleTemplates.length} 个模板</span>
      </div>

      <section className="template-grid" aria-label="模板列表">
        {visibleTemplates.length === 0 ? (
          <div className="template-empty card"><FileJson size={30} /><h2>{serviceUnavailable ? '模板服务未连接' : canManageTemplates ? '还没有模板' : '还没有可用的模板'}</h2><p>{serviceUnavailable ? (canManageTemplates ? '模板服务暂不可用，请联系管理员检查服务状态，恢复后即可在这里管理可复用的案件字段。' : '模板服务暂不可用，请联系管理员检查服务状态。') : canManageTemplates ? '把高频审查场景保存下来，下一次新建案件可以直接套用。' : '模板由管理员维护。你可以先直接新建案件，或请管理员把常用场景加进来。'}</p>{canManageTemplates ? <button className="btn-primary" type="button" onClick={openCreate} disabled={serviceUnavailable}><Plus size={16} />新建第一个模板</button> : null}</div>
        ) : visibleTemplates.map((template) => (
          <article className="template-card card" key={template.id}>
            <div className="template-card__top"><h2>{template.name}</h2>{template.description ? <p>{template.description}</p> : null}</div>
            <div className="template-card__question"><p>{template.question}</p></div>
            <time className="template-card__date" dateTime={template.updated_at}>更新于 {formatDate(template.updated_at)}</time>
            <div className="template-card__actions"><button className="template-use" type="button" onClick={() => onUseTemplate?.(template)} disabled={!onUseTemplate || serviceUnavailable}>使用模板<ArrowRight size={17} aria-hidden="true" /></button>{canManageTemplates ? <><button className="btn-ghost template-icon-btn" type="button" title="编辑模板" aria-label="编辑模板" onClick={() => openEdit(template)} disabled={serviceUnavailable}><Pencil size={16} /></button><button className="btn-ghost template-icon-btn" type="button" title="导出 JSON" aria-label="导出 JSON" onClick={() => exportTemplate(template)} disabled={serviceUnavailable}><Download size={16} /></button><button className="btn-ghost template-icon-btn template-icon-btn--accent" type="button" title="归档模板" aria-label="归档模板" onClick={() => setArchiveTarget(template)} disabled={busy || serviceUnavailable}><Archive size={16} /></button></> : null}</div>
          </article>
        ))}
      </section>

      {canManageTemplates && editorOpen ? (
        <dialog
          ref={editorRef}
          className="template-editor card"
          aria-labelledby="template-editor-title"
          onClose={() => { if (!busy) closeEditor(); }}
          onClick={(event) => { if (event.target === editorRef.current) closeEditor(); }}
        >
          <div className="template-editor__header"><div><h2 id="template-editor-title">{editing ? '编辑模板' : '新建模板'}</h2></div><button className="btn-ghost template-icon-btn" type="button" aria-label="关闭编辑器" onClick={closeEditor}><X size={18} /></button></div>
          <label className="template-field"><span>模板名称</span><input value={draft.name} maxLength={120} onChange={(event) => updateDraft('name', event.target.value)} placeholder="例如：个人信息出境审查" /></label>
          <label className="template-field"><span>适用场景说明</span><input value={draft.description} maxLength={500} onChange={(event) => updateDraft('description', event.target.value)} placeholder="说明什么时候适合使用" /></label>
          <label className="template-field"><span>审查问题</span><textarea value={draft.question} maxLength={4000} onChange={(event) => updateDraft('question', event.target.value)} rows={4} placeholder="例如：这个业务是否需要数据出境安全评估？" /></label>

          <div className="template-editor__footer"><button className="btn-secondary" type="button" onClick={closeEditor} disabled={busy}>取消</button><button className="btn-primary" type="button" onClick={() => void save()} disabled={busy}>{busy ? '保存中…' : '保存模板'}</button></div>
        </dialog>
      ) : null}

      {archiveTarget ? <dialog
        ref={archiveDialogRef}
        className="template-confirm card"
        aria-labelledby="template-confirm-title"
        onClose={() => setArchiveTarget(null)}
      >
        <h2 id="template-confirm-title">确认归档“{archiveTarget.name}”？</h2>
        <p>归档后不会再出现在选择列表中。</p>
        <div className="template-confirm__actions"><button type="button" className="btn-secondary" onClick={() => setArchiveTarget(null)} disabled={busy}>取消</button><button type="button" className="btn-primary" disabled={busy} onClick={() => void archive(archiveTarget)}>确认归档</button></div>
      </dialog> : null}
    </section>
  );
}
