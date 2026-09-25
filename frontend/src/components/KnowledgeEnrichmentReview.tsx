import { useEffect, useState } from 'react';
import { approveKnowledgeEnrichmentJob, listKnowledgeEnrichmentJobs, rejectKnowledgeEnrichmentJob, retryKnowledgeEnrichmentJob } from '../api/client';
import type { KnowledgeEnrichmentJobApi } from '../types/api';

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

export default function KnowledgeEnrichmentReview(): JSX.Element {
  const [jobs, setJobs] = useState<KnowledgeEnrichmentJobApi[]>([]);
  const [selected, setSelected] = useState<KnowledgeEnrichmentJobApi | null>(null);
  const [form, setForm] = useState<ReviewForm | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = async (): Promise<void> => {
    try {
      setJobs(await listKnowledgeEnrichmentJobs());
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取待审法源');
    }
  };
  useEffect(() => { void refresh(); }, []);

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
  const awaiting = jobs.filter((job) => job.status === 'awaiting_review');
  const failed = jobs.filter((job) => job.status === 'failed');

  return (
    <section className="knowledge-trash card knowledge-enrichment-review" aria-label="新发现官方法源待审">
      <div className="knowledge-list__header"><h2>新发现法源待审</h2><span className="knowledge-list__count">{awaiting.length} 项</span></div>
      {error ? <div className="knowledge-alert knowledge-alert--error" role="alert">{error}</div> : null}
      {awaiting.length === 0 ? <p>当前没有待审来源。</p> : awaiting.map((job) => (
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
          <label className="form-field"><span>类型</span><select value={form.doc_type} onChange={(event) => setForm({ ...form, doc_type: event.target.value as ReviewForm['doc_type'] })}><option value="policy">政策通知</option><option value="faq">问答</option><option value="guideline">指南</option><option value="law">法律</option><option value="regulation">法规或规章</option></select></label>
          <label className="form-field"><span>效力层级</span><select value={form.authority} onChange={(event) => setForm({ ...form, authority: event.target.value as ReviewForm['authority'] })}><option value="unknown">待确认</option><option value="national_law">国家法律</option><option value="administrative_regulation">行政法规</option><option value="departmental_rule">部门规章</option><option value="ministry_policy">政策文件</option><option value="public_interpretation">公开解释</option></select></label>
        </div>
        <label className="form-field"><span>引用角色</span><select value={form.citation_role} onChange={(event) => setForm({ ...form, citation_role: event.target.value as ReviewForm['citation_role'] })}><option value="interpretation_auxiliary">解释参考</option><option value="implementation_reference">办理参考</option><option value="primary_legal_basis">正式条款依据</option></select></label>
        <div className="knowledge-form__grid">
          <label className="form-field"><span>发布机关</span><input value={form.issuing_body} onChange={(event) => setForm({ ...form, issuing_body: event.target.value })} /></label>
          <label className="form-field"><span>生效日期</span><input type="date" value={form.effective_date} onChange={(event) => setForm({ ...form, effective_date: event.target.value })} /></label>
        </div>
        <label className="form-field"><span>效力状态</span><select value={form.law_status} onChange={(event) => setForm({ ...form, law_status: event.target.value as ReviewForm['law_status'] })}><option value="unknown">待确认</option><option value="effective">现行有效</option><option value="not_yet_effective">尚未生效</option></select></label>
        <div className="knowledge-detail__actions"><button type="button" className="btn-primary" disabled={busy || !valid} onClick={() => void decide(true)}>批准入库</button><button type="button" className="btn-secondary" disabled={busy} onClick={() => void decide(false)}>拒绝</button></div>
      </div> : null}
    </section>
  );
}
