import { useEffect, useRef, useState } from 'react';
import { ScanSearch, X, ChevronRight } from 'lucide-react';
import { approveKnowledgeEnrichmentJob, listKnowledgeEnrichmentJobs, rejectKnowledgeEnrichmentJob, retryKnowledgeEnrichmentJob } from '../api/client';
import type { KnowledgeEnrichmentJobApi } from '../types/api';
import { AUTHORITY_LABELS, DOC_TYPE_LABELS, LAW_STATUS_LABELS, CITATION_ROLE_LABELS } from '../utils/display';

type ReviewForm = {
  title: string;
  doc_type: 'law' | 'regulation' | 'policy' | 'faq' | 'guideline';
  authority: 'unknown' | 'national_law' | 'administrative_regulation' | 'departmental_rule' | 'ministry_policy' | 'public_interpretation';
  citation_role: 'primary_legal_basis' | 'implementation_reference' | 'interpretation_auxiliary';
  issuing_body: string;
  effective_date: string;
  law_status: 'effective' | 'not_yet_effective' | 'unknown';
};

function initialForm(job: KnowledgeEnrichmentJobApi): ReviewForm {
  return {
    title: job.title, doc_type: 'policy', authority: 'unknown',
    citation_role: 'interpretation_auxiliary', issuing_body: '',
    effective_date: '', law_status: 'unknown',
  };
}

/**
 * 新发现法源的待审队列。默认不占版面：有待审或失败任务、或读取失败时，
 * 在知识库页显示一条轻量提醒，点击后在原生 dialog 中完成核对与批准。
 */
export default function KnowledgeEnrichmentReview(): JSX.Element | null {
  const [jobs, setJobs] = useState<KnowledgeEnrichmentJobApi[]>([]);
  const [selected, setSelected] = useState<KnowledgeEnrichmentJobApi | null>(null);
  const [form, setForm] = useState<ReviewForm | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reviewOpen, setReviewOpen] = useState(false);
  const dialogRef = useRef<HTMLDialogElement>(null);

  const refresh = async (): Promise<void> => {
    try {
      setJobs(await listKnowledgeEnrichmentJobs());
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取待审法源');
    }
  };
  useEffect(() => { void refresh(); }, []);

  const awaiting = jobs.filter((job) => job.status === 'awaiting_review');
  const failed = jobs.filter((job) => job.status === 'failed');
  const hasPending = error !== null || awaiting.length > 0 || failed.length > 0;

  // 审核弹窗：原生 dialog 提供顶层渲染、Esc 关闭与焦点圈定
  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;
    if (reviewOpen) {
      if (!dialog.open) dialog.showModal();
    } else if (dialog.open) {
      dialog.close();
    }
  }, [reviewOpen]);

  const decide = async (approve: boolean): Promise<void> => {
    if (!selected || !form) return;
    setBusy(true);
    setError(null);
    try {
      if (approve) {
        await approveKnowledgeEnrichmentJob(selected.id, {
          source_id: `legal_${selected.id.replace(/^enrich_/, '')}`,
          library_kind: 'legal', title: form.title.trim(),
          source_url: selected.url, source_site: new URL(selected.url).hostname,
          doc_type: form.doc_type, authority: form.authority,
          citation_role: form.citation_role, law_status: form.law_status,
          effective_date: form.effective_date || null,
          issuing_body: form.issuing_body.trim() || null,
          file_format: (['pdf', 'docx', 'txt', 'html', 'htm'].includes(new URL(selected.url).pathname.split('.').pop()?.toLowerCase() || '')
            ? new URL(selected.url).pathname.split('.').pop()?.toLowerCase() : 'html'),
        });
      } else {
        await rejectKnowledgeEnrichmentJob(selected.id);
      }
      setSelected(null);
      setForm(null);
      await refresh();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '处理待审法源失败');
    } finally {
      setBusy(false);
    }
  };

  const primary = form?.citation_role === 'primary_legal_basis';
  const valid = Boolean(
    form?.title.trim() && form?.issuing_body.trim()
    && (!primary || (form?.effective_date && ['national_law', 'administrative_regulation', 'departmental_rule'].includes(form.authority)))
  );

  if (!hasPending) return null;

  return (
    <>
      <button type="button" className="knowledge-review-strip" onClick={() => setReviewOpen(true)}>
        <ScanSearch size={17} strokeWidth={1.8} aria-hidden="true" />
        <span>
          {error ?? `有 ${awaiting.length} 条新发现法源待审核${failed.length ? `，另有 ${failed.length} 条采集失败` : ''}`}
        </span>
        <ChevronRight size={16} strokeWidth={1.8} aria-hidden="true" />
      </button>

      {reviewOpen ? <dialog
        ref={dialogRef}
        className="knowledge-modal knowledge-review-dialog"
        aria-labelledby="knowledge-review-title"
        onClose={() => setReviewOpen(false)}
      >
        <div className="knowledge-modal__header">
          <div><h2 id="knowledge-review-title">新发现法源待审</h2><p>核对元数据后批准入库，或拒绝采集结果。</p></div>
          <button type="button" className="icon-btn" aria-label="关闭审核窗口" onClick={() => setReviewOpen(false)} disabled={busy}><X size={18} /></button>
        </div>
        {error ? <div className="knowledge-alert knowledge-alert--error" role="alert">{error}</div> : null}
        {awaiting.map((job) => (
          <div key={job.id} className="knowledge-preview__row">
            <div><strong>{job.title}</strong><small><a href={job.url} target="_blank" rel="noreferrer">{job.url}</a></small></div>
            <button type="button" className="btn-secondary" onClick={() => { setSelected(job); setForm(initialForm(job)); }}>核对元数据</button>
          </div>
        ))}
        {failed.map((job) => <div key={job.id} className="knowledge-preview__row"><div><strong>{job.title}</strong><small>{job.error || '采集或解析失败'}</small></div><button type="button" className="btn-secondary" disabled={busy} onClick={() => void retryKnowledgeEnrichmentJob(job.id).then(refresh).catch((reason) => setError(reason instanceof Error ? reason.message : '重试失败'))}>重试</button></div>)}
        {selected && form ? <div className="knowledge-form">
          <div className="knowledge-form__heading"><strong>核对官方原文与法源类型</strong></div>
          <p><a href={selected.url} target="_blank" rel="noreferrer">{selected.title}</a></p>
          <p>原件指纹：<code>{selected.raw_sha256 || '未记录'}</code> · <a href={`/api/admin/knowledge-enrichment-jobs/${encodeURIComponent(selected.id)}/raw`}>下载核对原件</a></p>
          {selected.parsed_excerpt ? <pre className="knowledge-preview__text">{selected.parsed_excerpt}</pre> : null}
          <label className="form-field"><span>标题</span><input value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} /></label>
          <div className="knowledge-form__grid">
            <label className="form-field"><span>类型</span><select value={form.doc_type} onChange={(event) => setForm({ ...form, doc_type: event.target.value as ReviewForm['doc_type'] })}><option value="policy">{DOC_TYPE_LABELS.policy}</option><option value="faq">{DOC_TYPE_LABELS.faq}</option><option value="guideline">{DOC_TYPE_LABELS.guideline}</option><option value="law">{DOC_TYPE_LABELS.law}</option><option value="regulation">{DOC_TYPE_LABELS.regulation}</option></select></label>
            <label className="form-field"><span>效力层级</span><select value={form.authority} onChange={(event) => setForm({ ...form, authority: event.target.value as ReviewForm['authority'] })}><option value="unknown">待确认</option><option value="national_law">{AUTHORITY_LABELS.national_law}</option><option value="administrative_regulation">{AUTHORITY_LABELS.administrative_regulation}</option><option value="departmental_rule">{AUTHORITY_LABELS.departmental_rule}</option><option value="ministry_policy">{AUTHORITY_LABELS.ministry_policy}</option><option value="public_interpretation">{AUTHORITY_LABELS.public_interpretation}</option></select></label>
          </div>
          <label className="form-field"><span>引用角色</span><select value={form.citation_role} onChange={(event) => setForm({ ...form, citation_role: event.target.value as ReviewForm['citation_role'] })}><option value="interpretation_auxiliary">{CITATION_ROLE_LABELS.interpretation_auxiliary}</option><option value="implementation_reference">{CITATION_ROLE_LABELS.implementation_reference}</option><option value="primary_legal_basis">{CITATION_ROLE_LABELS.primary_legal_basis}</option></select></label>
          <div className="knowledge-form__grid">
            <label className="form-field"><span>发布机关</span><input value={form.issuing_body} onChange={(event) => setForm({ ...form, issuing_body: event.target.value })} /></label>
            <label className="form-field"><span>生效日期</span><input type="date" value={form.effective_date} onChange={(event) => setForm({ ...form, effective_date: event.target.value })} /></label>
          </div>
          <label className="form-field"><span>效力状态</span><select value={form.law_status} onChange={(event) => setForm({ ...form, law_status: event.target.value as ReviewForm['law_status'] })}><option value="unknown">待确认</option><option value="effective">{LAW_STATUS_LABELS.effective}</option><option value="not_yet_effective">{LAW_STATUS_LABELS.not_yet_effective}</option></select></label>
          <div className="knowledge-detail__actions"><button type="button" className="btn-primary" disabled={busy || !valid} onClick={() => void decide(true)}>批准入库</button><button type="button" className="btn-secondary" disabled={busy} onClick={() => void decide(false)}>拒绝</button></div>
        </div> : null}
      </dialog> : null}
    </>
  );
}
