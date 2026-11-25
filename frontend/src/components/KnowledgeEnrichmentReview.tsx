import { useEffect, useRef, useState } from 'react';
import { ScanSearch, X, ChevronRight } from 'lucide-react';
import { approveKnowledgeEnrichmentJob, listKnowledgeEnrichmentJobs, rejectKnowledgeEnrichmentJob, retryKnowledgeEnrichmentJob } from '../api/client';
import type { KnowledgeEnrichmentJobApi } from '../types/api';
import type { ClauseCitationRole } from '../types/api';
import { AUTHORITY_LABELS, DOC_TYPE_LABELS, LAW_STATUS_LABELS, CITATION_ROLE_LABELS } from '../utils/display';

type ReviewForm = {
  title: string;
  doc_type: string;
  authority: string;
  citation_role: ClauseCitationRole;
  applicable_region: string;
  issuing_body: string;
  publish_date: string;
  effective_date: string;
  law_status: string;
};

const NORM_DOC_TYPES = ['law', 'regulation', 'judicial_interpretation'];
const FIELD_LABELS: Record<string, string> = {
  doc_type: '类型', authority: '效力层级', citation_role: '引用角色', applicable_region: '适用地区',
  issuing_body: '发布机关', publish_date: '发布日期', effective_date: '生效日期', law_status: '效力状态',
};

/** 审核表单以采集任务推断出的草稿为初值；没有草稿的旧任务才从空白开始。 */
function initialForm(job: KnowledgeEnrichmentJobApi): ReviewForm {
  const draft = job.draft_json?.source;
  return {
    title: draft?.title ?? job.title,
    doc_type: draft?.doc_type ?? 'policy',
    authority: draft?.authority ?? 'unknown',
    citation_role: draft?.citation_role ?? 'interpretation_auxiliary',
    applicable_region: draft?.applicable_region ?? 'CN',
    issuing_body: draft?.issuing_body ?? '',
    publish_date: draft?.publish_date ?? '',
    effective_date: draft?.effective_date ?? '',
    law_status: draft?.law_status ?? 'unknown',
  };
}

function Evidence({ job, field }: { job: KnowledgeEnrichmentJobApi; field: string }): JSX.Element | null {
  const draft = job.draft_json;
  if (!draft) return null;
  if (draft.unresolved.includes(field)) return <small className="knowledge-form__unresolved">待确认，请核对填写</small>;
  const reason = draft.evidence[field];
  return reason ? <small className="knowledge-form__evidence">依据：{reason}</small> : null;
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
        const draft = selected.draft_json?.source;
        const extension = new URL(selected.url).pathname.split('.').pop()?.toLowerCase() || '';
        await approveKnowledgeEnrichmentJob(selected.id, {
          ...(draft ?? {}),
          source_id: `legal_${selected.id.replace(/^enrich_/, '')}`,
          library_kind: 'legal', title: form.title.trim(),
          source_url: selected.url, source_site: new URL(selected.url).hostname,
          doc_type: form.doc_type, authority: form.authority,
          citation_role: form.citation_role, law_status: form.law_status,
          applicable_region: form.applicable_region.trim() || 'CN',
          publish_date: form.publish_date || null,
          effective_date: form.effective_date || null,
          issuing_body: form.issuing_body.trim() || null,
          file_format: draft?.file_format ?? (['pdf', 'docx', 'txt', 'html', 'htm'].includes(extension) ? extension : 'html'),
          include_in_mvp: true,
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

  // 正式法规必须有生效日期；主要法规依据只能来自法律、行政法规、部门规章或司法解释
  const norm = form ? NORM_DOC_TYPES.includes(form.doc_type) : false;
  const primary = form?.citation_role === 'primary_legal_basis';
  const valid = Boolean(
    form?.title.trim()
    && (!norm || (form?.effective_date && form?.issuing_body.trim()))
    && (!primary || ['national_law', 'administrative_regulation', 'departmental_rule', 'judicial_interpretation'].includes(form?.authority ?? ''))
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
          {selected.draft_json?.reader === 'rules' ? <div className="knowledge-alert">模型不可用，以下元数据为规则推断，请逐项核对。</div> : null}
          {selected.draft_json?.unresolved.includes('page_kind') ? <div className="knowledge-alert">模型无法确定本页是文件本身还是只印发附件的通知；若正文在附件里，请驳回本页并改用附件地址补库。</div> : null}
          {selected.draft_json?.attachments?.length ? (
            <div className="knowledge-alert">
              本页附件：
              {selected.draft_json.attachments.map((item) => (
                <span key={item.url}> <a href={item.url} target="_blank" rel="noreferrer">{item.name}</a>{item.follow ? '（模型建议入库）' : ''}；</span>
              ))}
            </div>
          ) : null}
          {selected.draft_json?.evidence.note ? <div className="knowledge-alert">模型说明：{selected.draft_json.evidence.note}</div> : null}
          {selected.draft_json?.supersedes.length ? <div className="knowledge-alert">将取代：{selected.draft_json.supersedes.map((item) => `${item.title}（${item.date ?? '日期未知'}）`).join('；')}；批准后旧版自动设置失效日期。</div> : null}
          {selected.error ? <div className="knowledge-alert knowledge-alert--error">{selected.error}</div> : null}
          <label className="form-field"><span>标题</span><input value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} /></label>
          <div className="knowledge-form__grid">
            <label className="form-field"><span>{FIELD_LABELS.doc_type}</span><select value={form.doc_type} onChange={(event) => setForm({ ...form, doc_type: event.target.value })}>{Object.entries(DOC_TYPE_LABELS).filter(([value]) => !['unknown', 'internal_policy', 'privacy_policy', 'case'].includes(value)).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><Evidence job={selected} field="doc_type" /></label>
            <label className="form-field"><span>{FIELD_LABELS.authority}</span><select value={form.authority} onChange={(event) => setForm({ ...form, authority: event.target.value })}>{Object.entries(AUTHORITY_LABELS).filter(([value]) => !['privacy_policy', 'simulated_internal_policy'].includes(value)).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><Evidence job={selected} field="authority" /></label>
          </div>
          <div className="knowledge-form__grid">
            <label className="form-field"><span>{FIELD_LABELS.citation_role}</span><select value={form.citation_role} onChange={(event) => setForm({ ...form, citation_role: event.target.value as ClauseCitationRole })}>{Object.entries(CITATION_ROLE_LABELS).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select><Evidence job={selected} field="citation_role" /></label>
            <label className="form-field"><span>{FIELD_LABELS.applicable_region}</span><input value={form.applicable_region} onChange={(event) => setForm({ ...form, applicable_region: event.target.value })} /><Evidence job={selected} field="applicable_region" /></label>
          </div>
          <div className="knowledge-form__grid">
            <label className="form-field"><span>{FIELD_LABELS.issuing_body}</span><input value={form.issuing_body} onChange={(event) => setForm({ ...form, issuing_body: event.target.value })} /><Evidence job={selected} field="issuing_body" /></label>
            <label className="form-field"><span>{FIELD_LABELS.law_status}</span><select value={form.law_status} onChange={(event) => setForm({ ...form, law_status: event.target.value })}><option value="unknown">待确认</option><option value="effective">{LAW_STATUS_LABELS.effective}</option><option value="not_yet_effective">{LAW_STATUS_LABELS.not_yet_effective}</option></select></label>
          </div>
          <div className="knowledge-form__grid">
            <label className="form-field"><span>{FIELD_LABELS.publish_date}</span><input type="date" value={form.publish_date} onChange={(event) => setForm({ ...form, publish_date: event.target.value })} /><Evidence job={selected} field="publish_date" /></label>
            <label className="form-field"><span>{FIELD_LABELS.effective_date}</span><input type="date" value={form.effective_date} onChange={(event) => setForm({ ...form, effective_date: event.target.value })} /><Evidence job={selected} field="effective_date" /></label>
          </div>
          {selected.draft_json?.chunks.length ? <details className="knowledge-form__chunks"><summary>切块预览（共 {selected.draft_json.chunk_count} 块）</summary>{selected.draft_json.chunks.map((chunk, index) => <div key={index}><small>{chunk.heading}</small><pre className="knowledge-preview__text">{chunk.text}</pre></div>)}</details> : null}
          <div className="knowledge-detail__actions"><button type="button" className="btn-primary" disabled={busy || !valid} onClick={() => void decide(true)}>批准入库</button><button type="button" className="btn-secondary" disabled={busy} onClick={() => void decide(false)}>拒绝</button></div>
        </div> : null}
      </dialog> : null}
    </>
  );
}
