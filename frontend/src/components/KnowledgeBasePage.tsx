import { useEffect, useMemo, useRef, useState } from 'react';
import {
  Archive,
  ArrowDownToLine,
  Check,
  CheckCircle2,
  ChevronRight,
  FileArchive,
  FilePlus2,
  FileText,
  LoaderCircle,
  Pencil,
  RefreshCw,
  RotateCcw,
  Search,
  Trash2,
  UploadCloud,
  X,
} from 'lucide-react';
import {
  commitKnowledgeDelete,
  commitKnowledgeImport,
  getKnowledgeSource,
  knowledgeSourceDownloadUrl,
  listKnowledgeSources,
  listKnowledgeTrash,
  previewKnowledgeDelete,
  previewKnowledgeImport,
  restoreKnowledgeSource,
  updateKnowledgeMetadata,
  waitForKnowledgeJob,
} from '../api/client';
import type {
  KnowledgeDeletePreviewApi,
  KnowledgeImportAction,
  KnowledgeImportPreviewApi,
  KnowledgeInternalStatus,
  KnowledgeJobApi,
  KnowledgeLawStatus,
  KnowledgeLibraryKind,
  KnowledgeSourceApi,
  KnowledgeSourceDetailApi,
  KnowledgeTrashRecordApi,
  WorkbenchUser,
} from '../types/api';

interface KnowledgeBasePageProps {
  user: WorkbenchUser;
  initialLibraryKind: KnowledgeLibraryKind;
}

interface MetadataForm {
  title: string;
  source_url: string;
  source_site: string;
  issuing_body: string;
  owning_department: string;
  publish_date: string;
  effective_date: string;
  law_status: KnowledgeLawStatus;
  internal_status: KnowledgeInternalStatus;
}

const LIBRARY_LABELS: Record<KnowledgeLibraryKind, { label: string; short: string; description: string }> = {
  legal: { label: '法律法规', short: '法源', description: '国家法律、行政法规、部门规章与权威公开解释。' },
  internal_policy: { label: '规章制度', short: '制度', description: '企业内部制度、流程规范与已经批准的政策文件。' },
};

const LAW_STATUS_LABELS: Record<KnowledgeLawStatus, string> = {
  effective: '现行有效',
  not_yet_effective: '尚未生效',
  amended: '已修订',
  repealed: '已废止',
  unknown: '待确认',
};

const INTERNAL_STATUS_LABELS: Record<KnowledgeInternalStatus, string> = {
  draft: '草稿',
  effective: '生效中',
  retired: '已停用',
};

const ACTION_LABELS: Record<KnowledgeImportAction, string> = {
  add: '新增',
  replace: '替换',
  skip: '跳过',
  error: '解析失败',
};

function formatBytes(value: number | null | undefined): string {
  if (!value || value < 1024) return `${value ?? 0} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

function formatDate(value: string | null | undefined): string {
  if (!value) return '—';
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleDateString('zh-CN');
}

function statusText(source: KnowledgeSourceApi): string {
  if (source.source.library_kind === 'internal_policy') {
    return INTERNAL_STATUS_LABELS[source.source.internal_status ?? 'draft'];
  }
  return LAW_STATUS_LABELS[source.source.law_status] ?? '待确认';
}

function sourceMeta(source: KnowledgeSourceApi): string {
  const owner = source.source.library_kind === 'internal_policy'
    ? source.source.owning_department || '未设归属部门'
    : source.source.issuing_body || '未设发布机关';
  return `${owner} · ${source.chunk_count} 个知识片段`;
}

function formFromSource(source: KnowledgeSourceDetailApi): MetadataForm {
  return {
    title: source.source.title,
    source_url: source.source.source_url,
    source_site: source.source.source_site,
    issuing_body: source.source.issuing_body ?? '',
    owning_department: source.source.owning_department ?? '',
    publish_date: source.source.publish_date ?? '',
    effective_date: source.source.effective_date ?? '',
    law_status: source.source.law_status,
    internal_status: source.source.internal_status ?? 'draft',
  };
}

function actionClass(action: KnowledgeImportAction): string {
  return `knowledge-action knowledge-action--${action}`;
}

export default function KnowledgeBasePage({ user, initialLibraryKind }: KnowledgeBasePageProps): JSX.Element {
  const [libraryKind, setLibraryKind] = useState<KnowledgeLibraryKind>(initialLibraryKind);
  const [sources, setSources] = useState<KnowledgeSourceApi[]>([]);
  const [trash, setTrash] = useState<KnowledgeTrashRecordApi[]>([]);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<KnowledgeSourceDetailApi | null>(null);
  const [editForm, setEditForm] = useState<MetadataForm | null>(null);
  const [searchInput, setSearchInput] = useState('');
  const [query, setQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState('');
  const [loading, setLoading] = useState(true);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [showTrash, setShowTrash] = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [importFiles, setImportFiles] = useState<File[]>([]);
  const [importDefaults, setImportDefaults] = useState({ source_site: '', issuing_body: '', owning_department: '', internal_status: 'draft' as KnowledgeInternalStatus });
  const [importPreview, setImportPreview] = useState<KnowledgeImportPreviewApi | null>(null);
  const [deletePreview, setDeletePreview] = useState<KnowledgeDeletePreviewApi | null>(null);
  const [deleteConfirmation, setDeleteConfirmation] = useState('');
  const [job, setJob] = useState<KnowledgeJobApi | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => setLibraryKind(initialLibraryKind), [initialLibraryKind]);

  useEffect(() => {
    if (!showImport && !deletePreview) return undefined;
    const handleEscape = (event: KeyboardEvent): void => {
      if (event.key !== 'Escape' || busy) return;
      setShowImport(false);
      setDeletePreview(null);
    };
    window.addEventListener('keydown', handleEscape);
    return () => window.removeEventListener('keydown', handleEscape);
  }, [busy, deletePreview, showImport]);

  const refresh = async (keepDetail = true): Promise<void> => {
    setLoading(true);
    setError(null);
    try {
      const [sourceResult, trashResult] = await Promise.all([
        listKnowledgeSources(libraryKind, { query, status: statusFilter }),
        listKnowledgeTrash(libraryKind),
      ]);
      setSources(sourceResult.items);
      setTrash(trashResult.items);
      setSelectedIds((current) => new Set([...current].filter((id) => sourceResult.items.some((item) => item.source.source_id === id))));
      if (keepDetail && selectedId) {
        try {
          const nextDetail = await getKnowledgeSource(selectedId);
          setDetail(nextDetail);
          setEditForm(formFromSource(nextDetail));
        } catch {
          setSelectedId(null);
          setDetail(null);
          setEditForm(null);
        }
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法加载知识库来源');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    setSelectedIds(new Set());
    setSelectedId(null);
    setDetail(null);
    setEditForm(null);
    void refresh(false);
    // Refresh intentionally follows the selected library and explicit filters.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [libraryKind, query, statusFilter]);

  const runJob = async (jobToRun: KnowledgeJobApi | undefined, success: string): Promise<void> => {
    if (!jobToRun?.id) {
      setJob(jobToRun ?? null);
      setMessage(success);
      await refresh();
      return;
    }
    setJob(jobToRun);
    const finished = await waitForKnowledgeJob(jobToRun.id, setJob);
    setJob(finished);
    if (finished.status === 'failed') throw new Error(finished.error || '知识库任务失败');
    setMessage(success);
    await refresh();
  };

  const openSource = async (sourceId: string): Promise<void> => {
    setSelectedId(sourceId);
    setDetailLoading(true);
    setError(null);
    try {
      const nextDetail = await getKnowledgeSource(sourceId);
      setDetail(nextDetail);
      setEditForm(formFromSource(nextDetail));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取来源详情');
    } finally {
      setDetailLoading(false);
    }
  };

  const toggleSelected = (sourceId: string): void => {
    setSelectedIds((current) => {
      const next = new Set(current);
      if (next.has(sourceId)) next.delete(sourceId); else next.add(sourceId);
      return next;
    });
  };

  const toggleAll = (): void => {
    setSelectedIds((current) => current.size === sources.length
      ? new Set()
      : new Set(sources.map((item) => item.source.source_id)));
  };

  const startDeletePreview = async (): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      setDeletePreview(await previewKnowledgeDelete(libraryKind, [...selectedIds]));
      setDeleteConfirmation('');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法生成删除预检');
    } finally {
      setBusy(false);
    }
  };

  const confirmDelete = async (): Promise<void> => {
    if (!deletePreview) return;
    setBusy(true);
    setError(null);
    try {
      const response = await commitKnowledgeDelete(deletePreview.token, deleteConfirmation);
      setDeletePreview(null);
      setSelectedIds(new Set());
      await runJob(response.job, '来源已移入回收站，保留 30 天。');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '删除任务提交失败');
    } finally {
      setBusy(false);
    }
  };

  const runImportPreview = async (): Promise<void> => {
    if (importFiles.length === 0) return;
    setBusy(true);
    setError(null);
    try {
      const metadata = importFiles.map(() => ({
        source_site: importDefaults.source_site.trim() || undefined,
        issuing_body: importDefaults.issuing_body.trim() || undefined,
        owning_department: importDefaults.owning_department.trim() || undefined,
        internal_status: libraryKind === 'internal_policy' ? importDefaults.internal_status : undefined,
      }));
      setImportPreview(await previewKnowledgeImport(libraryKind, importFiles, metadata));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '导入预检失败');
    } finally {
      setBusy(false);
    }
  };

  const commitImport = async (): Promise<void> => {
    if (!importPreview) return;
    setBusy(true);
    setError(null);
    try {
      const response = await commitKnowledgeImport(importPreview.preview_id);
      setShowImport(false);
      setImportPreview(null);
      setImportFiles([]);
      await runJob(response.job, '导入任务已完成，知识库已刷新。');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '导入任务提交失败');
    } finally {
      setBusy(false);
    }
  };

  const saveMetadata = async (): Promise<void> => {
    if (!detail || !editForm) return;
    setBusy(true);
    setError(null);
    try {
      const payload: Record<string, unknown> = {
        title: editForm.title.trim(),
        source_url: editForm.source_url.trim() || null,
        source_site: editForm.source_site.trim() || null,
        issuing_body: editForm.issuing_body.trim() || null,
        owning_department: editForm.owning_department.trim() || null,
        publish_date: editForm.publish_date.trim() || null,
        effective_date: editForm.effective_date.trim() || null,
        topic_tags: detail.source.topic_tags,
      };
      if (libraryKind === 'internal_policy') payload.internal_status = editForm.internal_status;
      else payload.law_status = editForm.law_status;
      const response = await updateKnowledgeMetadata(detail.source.source_id, payload);
      await runJob(response.job, '来源元数据已更新，原文内容保持不变。');
      if (selectedId) await openSource(selectedId);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '元数据更新失败');
    } finally {
      setBusy(false);
    }
  };

  const restore = async (sourceId: string): Promise<void> => {
    setBusy(true);
    setError(null);
    try {
      const response = await restoreKnowledgeSource(sourceId, libraryKind);
      await runJob(response.job, '来源已恢复到知识库。');
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '恢复任务失败');
    } finally {
      setBusy(false);
    }
  };

  const summary = useMemo(() => ({
    sourceCount: sources.length,
    chunkCount: sources.reduce((total, item) => total + item.chunk_count, 0),
    selectedCount: selectedIds.size,
  }), [sources, selectedIds]);

  if (user.role !== 'admin') {
    return <section className="knowledge-main"><div className="card state-block"><h2>仅管理员可管理知识库</h2><p>知识库的导入、删除和元数据更新会触发受控后台任务。</p></div></section>;
  }

  return (
    <section className="knowledge-main" aria-labelledby="knowledge-title">
      <header className="knowledge-intro">
        <div className="knowledge-intro__copy">
          <h1 id="knowledge-title" className="page-title">知识库</h1>
          <p>集中维护可检索来源，保留导入、元数据和回收操作的完整轨迹。</p>
        </div>
      </header>

      <div className="knowledge-tabs" role="tablist" aria-label="知识库类型">
        {(Object.keys(LIBRARY_LABELS) as KnowledgeLibraryKind[]).map((kind) => (
          <button key={kind} type="button" role="tab" aria-selected={libraryKind === kind} className={libraryKind === kind ? 'is-active' : ''} onClick={() => setLibraryKind(kind)}>
            <span>{LIBRARY_LABELS[kind].label}</span>
          </button>
        ))}
      </div>

      <section className="knowledge-overview" aria-label="知识库概览">
        <div className="knowledge-overview__copy"><span>当前库</span><strong>{LIBRARY_LABELS[libraryKind].label}</strong><p>{LIBRARY_LABELS[libraryKind].description}</p></div>
        <div className="knowledge-metrics"><div><strong>{summary.sourceCount}</strong><span>当前来源</span></div><div><strong>{summary.chunkCount}</strong><span>知识片段</span></div><div><strong>{trash.length}</strong><span>待处理回收</span></div></div>
      </section>

      <section className="knowledge-toolbar card" aria-label="知识库筛选和操作">
        <div className="knowledge-search"><Search size={17} strokeWidth={1.8} /><input value={searchInput} onChange={(event) => setSearchInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter') setQuery(searchInput); }} placeholder="搜索标题、来源 ID、发布机关或归属部门" /><button type="button" className="btn-secondary" onClick={() => setQuery(searchInput)}>检索</button></div>
        <select value={statusFilter} onChange={(event) => setStatusFilter(event.target.value)} aria-label="按状态筛选">
          <option value="">全部状态</option>
          {libraryKind === 'legal' ? Object.entries(LAW_STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>) : Object.entries(INTERNAL_STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select>
        <button type="button" className="btn-secondary" onClick={() => void refresh()} disabled={loading || busy}><RefreshCw size={15} />刷新</button>
        <button type="button" className="btn-primary" onClick={() => { setImportPreview(null); setImportFiles([]); setShowImport(true); }}><UploadCloud size={16} />导入来源</button>
      </section>

      {error ? <div className="knowledge-alert knowledge-alert--error" role="alert"><X size={16} />{error}</div> : null}
      {message ? <div className="knowledge-alert knowledge-alert--success" role="status"><CheckCircle2 size={16} />{message}{job ? <span className="knowledge-alert__job">任务 {job.status}</span> : null}</div> : null}

      <section className="knowledge-layout">
        <div className="knowledge-list card">
          <div className="knowledge-list__header"><h2>来源清单</h2><span className="knowledge-list__count">{summary.sourceCount} 项</span></div>
          <div className="knowledge-bulkbar"><label><input type="checkbox" checked={sources.length > 0 && selectedIds.size === sources.length} onChange={toggleAll} />全选当前结果</label><span>{summary.selectedCount ? `已选 ${summary.selectedCount} 项` : ''}</span><button type="button" className="knowledge-danger-btn" disabled={selectedIds.size === 0 || busy} onClick={() => void startDeletePreview()}><Trash2 size={15} />移入回收站</button></div>
          {loading ? <div className="knowledge-state"><LoaderCircle className="spin" size={22} /><span>正在读取来源清单…</span></div> : sources.length === 0 ? <div className="knowledge-state"><FileArchive size={27} /><strong>暂无来源</strong><button type="button" className="btn-primary" onClick={() => setShowImport(true)}><FilePlus2 size={15} />开始导入</button></div> : <div className="knowledge-source-list">{sources.map((item) => <div key={item.source.source_id} className={'knowledge-source-row' + (selectedId === item.source.source_id ? ' is-open' : '')} role="button" aria-pressed={selectedId === item.source.source_id} tabIndex={0} onClick={() => void openSource(item.source.source_id)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); void openSource(item.source.source_id); } }}><input type="checkbox" checked={selectedIds.has(item.source.source_id)} onClick={(event) => event.stopPropagation()} onChange={() => toggleSelected(item.source.source_id)} aria-label={`选择${item.source.title}`} /><div className="knowledge-source-row__body"><div className="knowledge-source-row__title"><strong>{item.source.title}</strong><span className={`knowledge-status knowledge-status--${item.source.library_kind}`}>{statusText(item)}</span></div><p>{sourceMeta(item)}</p><code>{item.source.source_id}</code></div><ChevronRight size={17} className="knowledge-source-row__arrow" /></div>)}</div>}
        </div>

        <aside className="knowledge-detail card" aria-label="来源详情">
          {detailLoading ? <div className="knowledge-state"><LoaderCircle className="spin" size={22} /><span>正在读取来源详情…</span></div> : detail && editForm ? <><div className="knowledge-detail__header"><div><h2>{detail.source.title}</h2><code>{detail.source.source_id}</code></div><button type="button" className="icon-btn" aria-label="关闭来源详情" onClick={() => { setSelectedId(null); setDetail(null); setEditForm(null); }}><X size={17} /></button></div><div className="knowledge-detail__facts"><div><span>知识片段</span><strong>{detail.chunk_count}</strong></div><div><span>文件格式</span><strong>{detail.raw_format || detail.source.file_format}</strong></div><div><span>原文件</span><strong>{formatBytes(detail.raw_size)}</strong></div></div><div className="knowledge-detail__source-link"><span>具体来源</span>{detail.source.source_url ? <a href={detail.source.source_url} target="_blank" rel="noreferrer">{detail.source.source_url}</a> : <span className="knowledge-detail__source-empty">未提供 URL</span>}<small>{detail.source.source_site || '未填写来源站点'}</small></div><div className="knowledge-detail__actions"><a className="btn-secondary" href={knowledgeSourceDownloadUrl(detail.source.source_id)}><ArrowDownToLine size={15} />下载原文件</a></div><div className="knowledge-form"><div className="knowledge-form__heading"><strong>元数据维护</strong><Pencil size={15} /></div><label className="form-field"><span>标题</span><input value={editForm.title} onChange={(event) => setEditForm({ ...editForm, title: event.target.value })} /></label><label className="form-field"><span>来源站点</span><input value={editForm.source_site} onChange={(event) => setEditForm({ ...editForm, source_site: event.target.value })} /></label><label className="form-field"><span>来源 URL</span><input value={editForm.source_url} onChange={(event) => setEditForm({ ...editForm, source_url: event.target.value })} /></label><div className="knowledge-form__grid"><label className="form-field"><span>{libraryKind === 'legal' ? '发布机关' : '归属部门'}</span><input value={libraryKind === 'legal' ? editForm.issuing_body : editForm.owning_department} onChange={(event) => setEditForm({ ...editForm, [libraryKind === 'legal' ? 'issuing_body' : 'owning_department']: event.target.value })} /></label><label className="form-field"><span>{libraryKind === 'legal' ? '生效日期' : '制度生效日'}</span><input type="date" value={editForm.effective_date} onChange={(event) => setEditForm({ ...editForm, effective_date: event.target.value })} /></label></div>{libraryKind === 'legal' ? <label className="form-field"><span>法源状态</span><select value={editForm.law_status} onChange={(event) => setEditForm({ ...editForm, law_status: event.target.value as KnowledgeLawStatus })}>{Object.entries(LAW_STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label> : <label className="form-field"><span>制度状态</span><select value={editForm.internal_status} onChange={(event) => setEditForm({ ...editForm, internal_status: event.target.value as KnowledgeInternalStatus })}>{Object.entries(INTERNAL_STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>}<button type="button" className="btn-primary knowledge-form__save" disabled={busy || !editForm.title.trim()} onClick={() => void saveMetadata()}><Check size={15} />保存元数据</button></div><div className="knowledge-detail__technical"><span>内容指纹</span><code>{detail.content_hash || '尚未生成'}</code><span>生成批次</span><code>{detail.generation_id || '尚未生成'}</code></div></> : <div className="knowledge-state knowledge-state--detail"><FileText size={27} /><strong>选择一个来源</strong></div>}
        </aside>
      </section>

      <section className="knowledge-trash card"><button type="button" className="knowledge-trash__toggle" aria-expanded={showTrash} aria-controls="knowledge-trash-list" onClick={() => setShowTrash((value) => !value)}><Archive size={16} /><span>回收站</span><small>{trash.length} 项 · 30 天后自动清理</small><ChevronRight size={16} className={showTrash ? 'rotate-90' : ''} /></button>{showTrash ? <div id="knowledge-trash-list" className="knowledge-trash__list">{trash.length === 0 ? <p>当前没有待处理来源。</p> : trash.map((item) => <div key={`${item.source_id}-${item.trashed_at}`}><div><strong>{item.title}</strong><span>{item.source_id} · 移入 {formatDate(item.trashed_at)} · 到期 {formatDate(item.expires_at)}</span></div><button type="button" className="btn-secondary" disabled={busy} onClick={() => void restore(item.source_id)}><RotateCcw size={14} />恢复</button></div>)}</div> : null}</section>

      {showImport ? <div className="knowledge-modal-backdrop" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) setShowImport(false); }}><section className="knowledge-modal" role="dialog" aria-modal="true" aria-labelledby="knowledge-import-title"><div className="knowledge-modal__header"><div><h2 id="knowledge-import-title">导入{LIBRARY_LABELS[libraryKind].label}</h2><p>先预检，确认后写入知识库。</p></div><button type="button" className="icon-btn" aria-label="关闭导入窗口" onClick={() => setShowImport(false)} disabled={busy}><X size={18} /></button></div><div className="knowledge-import-drop"><input ref={fileInputRef} type="file" multiple accept=".txt,.md,.markdown,.pdf,.docx,.html,.htm,.json,.csv" onChange={(event) => setImportFiles(Array.from(event.target.files ?? []))} /><UploadCloud size={24} /><strong>{importFiles.length ? `已选择 ${importFiles.length} 个文件` : '选择本地文件'}</strong><span>支持 TXT、Markdown、PDF、DOCX、HTML、JSON、CSV，单文件不超过 20 MB。</span><button type="button" className="btn-secondary" onClick={() => fileInputRef.current?.click()}>浏览文件</button></div><div className="knowledge-form__grid knowledge-import-fields"><label className="form-field"><span>{libraryKind === 'legal' ? '发布机关（可选）' : '归属部门（可选）'}</span><input value={libraryKind === 'legal' ? importDefaults.issuing_body : importDefaults.owning_department} onChange={(event) => setImportDefaults({ ...importDefaults, [libraryKind === 'legal' ? 'issuing_body' : 'owning_department']: event.target.value })} placeholder={libraryKind === 'legal' ? '例如：全国人大常委会' : '例如：信息安全部'} /></label><label className="form-field"><span>来源站点（可选）</span><input value={importDefaults.source_site} onChange={(event) => setImportDefaults({ ...importDefaults, source_site: event.target.value })} placeholder="例如：公司制度中心" /></label>{libraryKind === 'internal_policy' ? <label className="form-field"><span>制度初始状态</span><select value={importDefaults.internal_status} onChange={(event) => setImportDefaults({ ...importDefaults, internal_status: event.target.value as KnowledgeInternalStatus })}>{Object.entries(INTERNAL_STATUS_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label> : null}</div><button type="button" className="btn-primary" disabled={busy || importFiles.length === 0} onClick={() => void runImportPreview()}><Search size={15} />生成导入预检</button>{importPreview ? <div className="knowledge-preview"><div className="knowledge-preview__summary"><strong>预检结果</strong><span>{importPreview.items.filter((item) => item.action === 'add').length} 新增 · {importPreview.items.filter((item) => item.action === 'replace').length} 替换 · {importPreview.items.filter((item) => item.action === 'skip').length} 跳过 · {importPreview.items.filter((item) => item.action === 'error').length} 失败</span></div>{importPreview.items.map((item) => <div key={item.id} className="knowledge-preview__row"><FileText size={15} /><div><strong>{item.filename}</strong><small>{item.source.title} · {formatBytes(item.size)}</small></div><span className={actionClass(item.action)}>{ACTION_LABELS[item.action]}</span>{item.error ? <em>{item.error}</em> : null}</div>)}<button type="button" className="btn-primary" disabled={busy || !importPreview.items.some((item) => item.action === 'add' || item.action === 'replace')} onClick={() => void commitImport()}><Check size={15} />确认写入知识库</button></div> : null}</section></div> : null}

      {deletePreview ? <div className="knowledge-modal-backdrop" role="presentation"><section className="knowledge-modal knowledge-modal--danger" role="dialog" aria-modal="true" aria-labelledby="knowledge-delete-title"><div className="knowledge-modal__header"><div><h2 id="knowledge-delete-title">确认移入回收站</h2><p>来源可在 30 天内恢复。</p></div><button type="button" className="icon-btn" aria-label="关闭删除确认" onClick={() => setDeletePreview(null)} disabled={busy}><X size={18} /></button></div><div className="knowledge-delete-summary"><strong>将处理 {deletePreview.items.length} 项来源</strong><span>共 {deletePreview.total_chunks} 个知识片段</span>{deletePreview.missing.length ? <small>未找到 {deletePreview.missing.length} 项：{deletePreview.missing.join('、')}</small> : null}</div><label className="form-field"><span>请输入确认语</span><input value={deleteConfirmation} onChange={(event) => setDeleteConfirmation(event.target.value)} placeholder={`删除 ${deletePreview.items.length} 项`} autoFocus /></label><button type="button" className="knowledge-danger-btn knowledge-danger-btn--solid" disabled={busy || deleteConfirmation !== `删除 ${deletePreview.items.length} 项`} onClick={() => void confirmDelete()}><Trash2 size={15} />确认删除</button></section></div> : null}
    </section>
  );
}
