import { useEffect, useMemo, useRef, useState } from 'react';
import type { ChangeEvent } from 'react';
import { Archive, Download, FileJson, Pencil, Plus, Search, Upload, X } from 'lucide-react';
import {
  archiveCaseTemplate,
  createCaseTemplate,
  listCaseTemplates,
  updateCaseTemplate,
  ApiError,
} from '../api/client';
import type { CaseIntake, CaseTemplateApi, CaseTemplatePayload } from '../types/api';
import './TemplateCenterPage.css';

const EMPTY_INTAKE: CaseIntake = {
  business_activity: '', data_types: [], sensitive_personal_info: null, cross_border_transfer: null,
  important_data_status: 'unknown', ciio_status: 'unknown', annual_non_sensitive_count: '',
  annual_sensitive_count: '', overseas_recipient: '', destination_region: '', processing_purpose: '',
  transfer_mechanism: '', vendor_name: '', contract_status: '', legal_basis_or_consent: '', notes: '',
};

const TEMPLATE_FORMAT = 'crosscomply.case-template';

export interface TemplateCenterPageProps {
  onUseTemplate?: (template: CaseTemplateApi) => void;
  demoMode?: boolean;
}

interface TemplateDraft {
  name: string;
  description: string;
  question: string;
  intake: CaseIntake;
  review_mode: 'llm' | 'multi_agent';
  rerank_mode: 'off' | 'embedding';
}

const EMPTY_DRAFT: TemplateDraft = {
  name: '', description: '', question: '', intake: { ...EMPTY_INTAKE }, review_mode: 'llm', rerank_mode: 'off',
};

function toDraft(template?: CaseTemplateApi | null): TemplateDraft {
  if (!template) return { ...EMPTY_DRAFT, intake: { ...EMPTY_INTAKE } };
  return {
    name: template.name,
    description: template.description,
    question: template.question,
    intake: { ...EMPTY_INTAKE, ...template.intake },
    review_mode: template.review_mode,
    rerank_mode: template.rerank_mode,
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
    review_mode: template.review_mode,
    rerank_mode: template.rerank_mode,
  };
}

export default function TemplateCenterPage({ onUseTemplate, demoMode = false }: TemplateCenterPageProps): JSX.Element {
  const [templates, setTemplates] = useState<CaseTemplateApi[]>([]);
  const [query, setQuery] = useState('');
  const [editing, setEditing] = useState<CaseTemplateApi | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [draft, setDraft] = useState<TemplateDraft>(toDraft());
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [serviceUnavailable, setServiceUnavailable] = useState(false);
  const importRef = useRef<HTMLInputElement>(null);

  const load = async (): Promise<void> => {
    const result = await listCaseTemplates(query);
    setTemplates(result.items);
    setServiceUnavailable(false);
  };

  useEffect(() => {
    void load().catch((reason) => {
      setServiceUnavailable(true);
      setMessage(
        demoMode
          ? '公开演示暂不提供模板数据，请连接自己的服务端后使用。'
          : reason instanceof ApiError && reason.status === 404
            ? '模板服务尚未就绪，请先执行 alembic upgrade head。'
            : reason instanceof Error ? reason.message : '无法加载模板',
      );
    });
  }, [demoMode, query]);

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
        intake: draft.intake, review_mode: draft.review_mode, rerank_mode: draft.rerank_mode,
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
    if (!window.confirm(`确认归档“${template.name}”？归档后不会再出现在选择列表中。`)) return;
    setBusy(true);
    try {
      await archiveCaseTemplate(template.id);
      await load();
      setMessage('模板已归档。');
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '归档模板失败');
    } finally {
      setBusy(false);
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
      if (!raw || typeof raw !== 'object') throw new Error('JSON 内容不是对象。');
      const value = raw as Partial<TemplateDraft> & { format?: unknown; version?: unknown };
      if (value.format !== TEMPLATE_FORMAT || value.version !== 1) throw new Error('不是有效的 CrossComply 使用模板文件。');
      if (typeof value.name !== 'string' || !value.name.trim() || typeof value.question !== 'string' || !value.question.trim()) {
        throw new Error('导入文件缺少模板名称或审查问题。');
      }
      const payload: CaseTemplatePayload = {
        name: `${value.name.trim()}（导入副本）`, description: typeof value.description === 'string' ? value.description : '',
        question: value.question.trim(), intake: { ...EMPTY_INTAKE, ...(value.intake && typeof value.intake === 'object' ? value.intake : {}) },
        review_mode: value.review_mode === 'multi_agent' ? 'multi_agent' : 'llm',
        rerank_mode: value.rerank_mode === 'embedding' ? 'embedding' : 'off',
      };
      setBusy(true);
      await createCaseTemplate(payload);
      await load();
      setMessage('模板已导入，并创建为副本。');
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '导入模板失败');
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="template-page" aria-labelledby="template-page-title">
      <header className="template-page__header">
        <div>
          <h1 id="template-page-title" className="page-title">使用模板</h1>
        </div>
        <div className="template-page__actions">
          <input ref={importRef} type="file" accept="application/json,.json" hidden onChange={(event) => void importTemplate(event)} />
          <button className="button button--secondary" type="button" onClick={() => importRef.current?.click()} disabled={busy || serviceUnavailable}><Upload size={16} />导入模板</button>
          <button className="button button--primary" type="button" onClick={openCreate} disabled={serviceUnavailable}><Plus size={17} />新建模板</button>
        </div>
      </header>

      {message && <div className="template-page__message" role="status">{message}</div>}

      <section className="template-toolbar card" aria-label="搜索模板">
        <Search size={18} aria-hidden="true" />
        <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索名称、适用场景或审查问题" aria-label="搜索模板" />
        <span>{visibleTemplates.length} 个可用模板</span>
      </section>

      <section className="template-grid" aria-label="模板列表">
        {visibleTemplates.length === 0 ? (
          <div className="template-empty card"><FileJson size={30} /><h2>{serviceUnavailable ? '模板服务未连接' : '还没有模板'}</h2><p>{serviceUnavailable ? '连接服务端并完成数据库升级后，即可在这里管理可复用的案件字段。' : '把高频审查场景保存下来，下一次新建案件可以直接套用。'}</p><button className="button button--primary" type="button" onClick={openCreate} disabled={serviceUnavailable}><Plus size={16} />新建第一个模板</button></div>
        ) : visibleTemplates.map((template) => (
          <article className="template-card card" key={template.id}>
            <div className="template-card__top"><div><h2>{template.name}</h2><p>{template.description || '未填写适用场景说明'}</p></div><span className="template-card__date">更新于 {formatDate(template.updated_at)}</span></div>
            <div className="template-card__question"><span>审查问题</span><strong>{template.question}</strong></div>
            <div className="template-card__meta"><span>审查方式：{template.review_mode === 'multi_agent' ? '多智能体' : '标准审查'}</span><span>字段预设：已保存</span></div>
            <div className="template-card__actions"><button className="button button--primary" type="button" onClick={() => onUseTemplate?.(template)} disabled={!onUseTemplate || serviceUnavailable}>使用此模板</button><button className="icon-button" type="button" title="编辑模板" aria-label="编辑模板" onClick={() => openEdit(template)} disabled={serviceUnavailable}><Pencil size={17} /></button><button className="icon-button" type="button" title="导出 JSON" aria-label="导出 JSON" onClick={() => exportTemplate(template)} disabled={serviceUnavailable}><Download size={17} /></button><button className="icon-button icon-button--danger" type="button" title="归档模板" aria-label="归档模板" onClick={() => void archive(template)} disabled={busy || serviceUnavailable}><Archive size={17} /></button></div>
          </article>
        ))}
      </section>

      {editorOpen ? (
        <div className="template-editor-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) closeEditor(); }}>
          <section className="template-editor card" role="dialog" aria-modal="true" aria-labelledby="template-editor-title">
            <div className="template-editor__header"><div><h2 id="template-editor-title">{editing ? '编辑模板' : '新建模板'}</h2></div><button className="icon-button" type="button" aria-label="关闭编辑器" onClick={closeEditor}><X size={18} /></button></div>
            <label className="template-field"><span>模板名称</span><input value={draft.name} maxLength={120} onChange={(event) => updateDraft('name', event.target.value)} placeholder="例如：个人信息出境审查" /></label>
            <label className="template-field"><span>适用场景说明</span><input value={draft.description} maxLength={500} onChange={(event) => updateDraft('description', event.target.value)} placeholder="说明什么时候适合使用" /></label>
            <label className="template-field"><span>审查问题</span><textarea value={draft.question} maxLength={4000} onChange={(event) => updateDraft('question', event.target.value)} rows={4} placeholder="例如：这个业务是否需要数据出境安全评估？" /></label>
            <div className="template-editor__grid"><label className="template-field"><span>审查方式</span><select value={draft.review_mode} onChange={(event) => updateDraft('review_mode', event.target.value as TemplateDraft['review_mode'])}><option value="llm">标准审查</option><option value="multi_agent">多智能体审查</option></select></label><label className="template-field"><span>依据排序</span><select value={draft.rerank_mode} onChange={(event) => updateDraft('rerank_mode', event.target.value as TemplateDraft['rerank_mode'])}><option value="off">默认排序</option><option value="embedding">增强排序</option></select></label></div>
            <p className="template-editor__note">这里只保存新建案件字段预设，不包含案件材料、审查结论、法源引用或审计记录。</p>
            <div className="template-editor__footer"><button className="button button--secondary" type="button" onClick={closeEditor} disabled={busy}>取消</button><button className="button button--primary" type="button" onClick={() => void save()} disabled={busy}>{busy ? '保存中…' : '保存模板'}</button></div>
          </section>
        </div>
      ) : null}
    </section>
  );
}
