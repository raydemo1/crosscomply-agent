import { useEffect, useMemo, useRef, useState } from 'react';
import type { MaterialEvidenceRef, ReviewAnnotationApi, ReviewIssue, ReviewMaterialApi } from '../types/api';
import {
  decideReviewAnnotation, followupReviewAnnotation, getReviewMaterials,
  listReviewAnnotations, saveReviewAnnotation,
} from '../api/client';
import './DocumentReview.css';

type LocatedItem = {
  id: string;
  start: number;
  end: number;
  kind: 'issue' | 'annotation';
  issue?: ReviewIssue;
  target?: MaterialEvidenceRef;
  annotation?: ReviewAnnotationApi;
};

interface DocumentReviewProps {
  caseId: string;
  reviewResultId: string;
  issues: ReviewIssue[];
  canManageActions: boolean;
  onRevisionTarget: (issue: ReviewIssue, target: MaterialEvidenceRef) => void;
  onPendingChange?: (count: number) => void;
}

function validIssueLocations(issues: ReviewIssue[], material: ReviewMaterialApi): LocatedItem[] {
  return issues.flatMap((issue) => issue.material_evidence
    .filter((target) => target.material_version_id === material.id
      && target.start_offset >= 0 && target.end_offset > target.start_offset
      && material.parsed_text.slice(target.start_offset, target.end_offset) === target.quote)
    .map((target) => ({
      id: `issue:${issue.id}:${target.start_offset}:${target.end_offset}`,
      start: target.start_offset, end: target.end_offset,
      kind: 'issue' as const, issue, target,
    })));
}

function validAnnotations(annotations: ReviewAnnotationApi[], material: ReviewMaterialApi, reviewResultId: string): LocatedItem[] {
  return annotations.filter((annotation) => annotation.review_result_id === reviewResultId
    && annotation.material_version_id === material.id && annotation.status !== 'rejected'
    && material.parsed_text.slice(annotation.start_offset, annotation.end_offset) === annotation.quote)
    .map((annotation) => ({
      id: `annotation:${annotation.id}`, start: annotation.start_offset, end: annotation.end_offset,
      kind: 'annotation', annotation,
    }));
}

function orderMaterials(materials: ReviewMaterialApi[], issues: ReviewIssue[]): ReviewMaterialApi[] {
  return [...materials].sort((left, right) =>
    validIssueLocations(issues, right).length - validIssueLocations(issues, left).length
    || right.parsed_text.length - left.parsed_text.length);
}

export default function DocumentReview({ caseId, reviewResultId, issues, canManageActions, onRevisionTarget, onPendingChange }: DocumentReviewProps): JSX.Element {
  const [materials, setMaterials] = useState<ReviewMaterialApi[]>([]);
  const [annotations, setAnnotations] = useState<ReviewAnnotationApi[]>([]);
  const [materialId, setMaterialId] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [selectedRange, setSelectedRange] = useState<{ start: number; end: number } | null>(null);
  const [finding, setFinding] = useState('');
  const [question, setQuestion] = useState('');
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const paperRef = useRef<HTMLDivElement>(null);
  const load = async (): Promise<void> => {
    const [nextMaterials, nextAnnotations] = await Promise.all([
      getReviewMaterials(caseId), listReviewAnnotations(caseId),
    ]);
    const ordered = orderMaterials(nextMaterials, issues);
    setMaterials(ordered);
    setAnnotations(nextAnnotations);
    setMaterialId((current) => current && ordered.some((item) => item.id === current) ? current : ordered[0]?.id ?? null);
  };
  useEffect(() => {
    let active = true;
    setLoaded(false);
    void Promise.all([getReviewMaterials(caseId), listReviewAnnotations(caseId)])
      .then(([nextMaterials, nextAnnotations]) => {
        if (!active) return;
        const ordered = orderMaterials(nextMaterials, issues);
        setMaterials(ordered);
        setAnnotations(nextAnnotations);
        setMaterialId(ordered[0]?.id ?? null);
        setError(null);
        setLoaded(true);
      })
      .catch((reason: unknown) => { if (active) { setError(reason instanceof Error ? reason.message : String(reason)); setLoaded(true); } });
    return () => { active = false; };
  }, [caseId, issues]);

  const material = materials.find((item) => item.id === materialId) ?? null;
  const locations = useMemo(() => material ? [
    ...validIssueLocations(issues, material), ...validAnnotations(annotations, material, reviewResultId),
  ] : [], [issues, annotations, material, reviewResultId]);
  const selected = locations.find((item) => item.id === selectedId) ?? null;
  const unresolvedIssues = issues.filter((issue) => !materials.some((item) => validIssueLocations([issue], item).length > 0));
  const pendingCount = annotations.filter((item) => item.review_result_id === reviewResultId && item.status === 'pending').length;
  useEffect(() => { onPendingChange?.(pendingCount); }, [pendingCount, onPendingChange]);

  const selectLocation = (item: LocatedItem, fromText = false): void => {
    setMaterialId(item.kind === 'issue' ? item.target!.material_version_id : item.annotation!.material_version_id);
    setSelectedRange(null);
    setSelectedId(item.id);
    window.requestAnimationFrame(() => {
      if (fromText && window.matchMedia('(max-width: 1279px)').matches) {
        document.querySelector('.document-review__detail')?.scrollIntoView({ block: 'start', behavior: 'smooth' });
        return;
      }
      const marks = paperRef.current?.querySelectorAll<HTMLElement>('mark[data-review-items]');
      const mark = Array.from(marks ?? []).find((node) => node.dataset.reviewItems?.split('|').includes(item.id));
      mark?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
  };

  const segments = useMemo(() => {
    if (!material) return [];
    const text = material.parsed_text;
    const points = Array.from(new Set([0, text.length, ...locations.flatMap((item) => [item.start, item.end])])).sort((a, b) => a - b);
    return points.slice(0, -1).map((start, index) => ({
      start, end: points[index + 1], text: text.slice(start, points[index + 1]),
      items: locations.filter((item) => item.start < points[index + 1] && item.end > start),
    }));
  }, [material, locations]);

  const captureSelection = (): void => {
    const selection = window.getSelection();
    const paper = paperRef.current;
    if (!selection || selection.isCollapsed || !paper || !selection.rangeCount) return;
    const range = selection.getRangeAt(0);
    if (!paper.contains(range.startContainer) || !paper.contains(range.endContainer)) return;
    const prefix = range.cloneRange();
    prefix.selectNodeContents(paper);
    prefix.setEnd(range.startContainer, range.startOffset);
    const start = prefix.toString().length;
    const end = start + range.toString().length;
    if (end > start && end - start <= 2000 && material?.parsed_text.slice(start, end) === range.toString()) {
      setSelectedRange({ start, end });
      setSelectedId(null);
    }
  };

  const mutate = async (action: () => Promise<unknown>): Promise<void> => {
    setBusy(true); setError(null);
    try { await action(); await load(); setSelectedRange(null); setFinding(''); setQuestion(''); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(false); }
  };
  const target = material && selectedRange ? {
    material_version_id: material.id, start_offset: selectedRange.start, end_offset: selectedRange.end,
  } : null;
  const overlapping = selected ? locations.filter((item) => item.start < selected.end && item.end > selected.start) : [];
  const reviseAnnotation = (annotation: ReviewAnnotationApi): void => {
    if (!material) return;
    const evidence: MaterialEvidenceRef = {
      material_version_id: material.id, logical_name: material.logical_name,
      filename: material.filename, version_number: material.version_number,
      quote: annotation.quote, start_offset: annotation.start_offset, end_offset: annotation.end_offset,
    };
    const issue: ReviewIssue = {
      id: annotation.id, kind: 'legal_gap', title: '补充批注', finding: annotation.finding,
      material_evidence: [evidence], supporting_chunk_ids: [],
      supporting_citation_refs: annotation.citation_refs, unknowns: [],
      recommended_action: annotation.recommendation,
    };
    onRevisionTarget(issue, evidence);
  };

  return <section className="document-review" aria-label="原文审阅">
    <div className="document-review__intro">
      <div><h2>原文审阅</h2><p>模型标记与人工批注落在本次审查的冻结原文上。选择文字可补充意见或发起追审。</p></div>
      <span>{issues.length} 项审查问题{pendingCount > 0 && canManageActions ? ` · ${pendingCount} 项待确认` : ''}</span>
    </div>
    {error ? <div className="document-review__error" role="alert">{error}</div> : null}
    {materials.length === 0 ? <div className="card document-review__empty">{error ? '原文加载失败，请刷新重试。' : loaded ? '本次审查没有可展示的解析材料。' : '正在读取本次审查的冻结材料…'}</div> : <>
      <div className="document-review__files" role="tablist" aria-label="冻结材料">
        {materials.map((item) => <button key={item.id} type="button" role="tab" aria-selected={item.id === materialId}
          className={item.id === materialId ? 'is-active' : ''}
          onClick={() => { setMaterialId(item.id); setSelectedId(null); setSelectedRange(null); }}>
          {item.logical_name} <small>v{item.version_number}</small>
        </button>)}
      </div>
      <div className="document-review__layout">
        <div className="document-review__paper-shell">
          <div className="document-review__paper-header"><strong>{material?.filename}</strong><span>冻结原文 · {material?.parsed_text.length.toLocaleString()} 字</span></div>
          <div className="document-review__paper" ref={paperRef} onMouseUp={captureSelection} onKeyUp={captureSelection} role="tabpanel" aria-label="冻结材料原文">
            {segments.map((segment) => segment.items.length === 0 ? <span key={segment.start}>{segment.text}</span> : <mark key={segment.start}
              data-review-items={segment.items.map((item) => item.id).join('|')}
              tabIndex={0} role="button" aria-label={`${segment.items.length} 条批注，点击查看`}
              className={`document-review__mark${segment.items.some((item) => item.id === selectedId) ? ' is-selected' : ''}${segment.items.some((item) => item.annotation?.status === 'pending') ? ' is-pending' : ''}`}
              onClick={() => { if (window.getSelection()?.isCollapsed) selectLocation(segment.items[0], true); }}
              onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectLocation(segment.items[0], true); } }}>
              {segment.text}</mark>)}
          </div>
        </div>
        <aside className="document-review__side" aria-label="原文批注与操作">
          {selectedRange && material ? <div className="document-review__detail">
            <h3>选中的原文</h3><blockquote>{material.parsed_text.slice(selectedRange.start, selectedRange.end)}</blockquote>
            {canManageActions ? <>
              <label>人工批注<textarea value={finding} onChange={(event) => setFinding(event.target.value)} rows={4} placeholder="写下需要注意的问题或处理意见" /></label>
              <button type="button" disabled={busy || !finding.trim()} onClick={() => target && void mutate(() => saveReviewAnnotation(caseId, target, finding.trim()))}>保存人工批注</button>
              <label>向模型追问<textarea value={question} onChange={(event) => setQuestion(event.target.value)} rows={3} placeholder="希望模型继续核查什么？" /></label>
              <button type="button" disabled={busy || !question.trim()} onClick={() => target && void mutate(() => followupReviewAnnotation(caseId, target, question.trim()))}>{busy ? '正在处理…' : '发起追审'}</button>
            </> : null}
          </div> : selected ? <div className="document-review__detail">
            <span className="document-review__source">{selected.kind === 'issue' ? '模型审查问题' : selected.annotation?.source === 'human' ? '人工批注' : '模型追审批注'}{selected.annotation?.status === 'pending' ? ' · 待确认' : ''}</span>
            {overlapping.length > 1 ? <div className="document-review__overlap" aria-label="同一位置的批注">
              {overlapping.map((item, index) => <button key={item.id} type="button" className={item.id === selected.id ? 'is-active' : ''} onClick={() => setSelectedId(item.id)}>批注 {index + 1}</button>)}
            </div> : null}
            <h3>{selected.issue?.title ?? '原文批注'}</h3>
            <blockquote>{selected.target?.quote ?? selected.annotation?.quote}</blockquote>
            <p>{selected.issue?.finding ?? selected.annotation?.finding}</p>
            {selected.annotation?.recommendation ? <p><strong>建议：</strong>{selected.annotation.recommendation}</p> : null}
            {selected.issue?.recommended_action ? <p><strong>建议：</strong>{selected.issue.recommended_action}</p> : null}
            {selected.annotation?.insufficient_evidence ? <p className="document-review__caution">证据不足，需要进一步核查。</p> : null}
            {selected.annotation?.citation_refs.length ? <p>法源：{selected.annotation.citation_refs.join('、')}</p> : null}
            {selected.annotation?.source === 'model' && selected.annotation.status === 'pending' && canManageActions ? <div className="document-review__decision">
              <button type="button" disabled={busy} onClick={() => void mutate(() => decideReviewAnnotation(selected.annotation!.id, 'rejected', selected.annotation!.version))}>驳回</button>
              <button type="button" disabled={busy} onClick={() => void mutate(() => decideReviewAnnotation(selected.annotation!.id, 'confirmed', selected.annotation!.version))}>确认批注</button>
            </div> : null}
            {selected.issue && selected.target && canManageActions && selected.issue.kind !== 'missing_information' ? <button type="button" onClick={() => onRevisionTarget(selected.issue!, selected.target!)}>针对这段准备修改</button> : null}
            {selected.annotation?.status === 'confirmed' && !selected.annotation.insufficient_evidence && canManageActions ? <button type="button" onClick={() => reviseAnnotation(selected.annotation!)}>根据批注准备修改</button> : null}
          </div> : <div className="document-review__detail document-review__detail--empty"><h3>从原文开始</h3><p>点击荧光标记查看模型发现的问题；也可以选中一段原文，追加人工批注或请模型追审。</p></div>}
          <div className="document-review__index"><h3>已定位的问题与批注</h3>
            {materials.flatMap((item) => [
              ...validIssueLocations(issues, item), ...validAnnotations(annotations, item, reviewResultId),
            ]).map((item) => <button key={item.id} type="button" onClick={() => selectLocation(item)}>
              <span>{item.kind === 'issue' ? '模型问题' : item.annotation?.status === 'pending' ? '待确认追审' : '补充批注'}</span>
              <strong>{item.issue?.title ?? item.annotation?.finding}</strong>
            </button>)}
          </div>
          {unresolvedIssues.length > 0 ? <div className="document-review__unlocated"><h3>无法定位到具体原文的问题</h3><ul>{unresolvedIssues.map((item) => <li key={item.id}>{item.title}</li>)}</ul></div> : null}
        </aside>
      </div>
    </>}
  </section>;
}
