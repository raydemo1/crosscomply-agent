import { useCallback, useEffect, useState } from 'react';
import type { MaterialEvidenceRef, RevisionProposalApi, ReviewIssue, WorkingDraftApi } from '../types/api';
import { decideRevisionProposal, generateRevisionProposal, getWorkingDraft, listRevisionProposals } from '../api/client';
import MarkdownText from './MarkdownText';

export interface RevisionSelection {
  issue: ReviewIssue;
  target: MaterialEvidenceRef;
}

interface RevisionWorkspaceProps {
  caseId: string;
  selection: RevisionSelection | null;
  canManageActions: boolean;
}

export default function RevisionWorkspace({ caseId, selection, canManageActions }: RevisionWorkspaceProps): JSX.Element | null {
  const [proposals, setProposals] = useState<RevisionProposalApi[]>([]);
  const [draft, setDraft] = useState<WorkingDraftApi | null>(null);
  const [editedText, setEditedText] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [view, setView] = useState<'annotated' | 'reading'>('annotated');

  const refresh = useCallback(async () => {
    const items = await listRevisionProposals(caseId);
    setProposals(items);
    if (selection) setDraft(await getWorkingDraft(caseId, selection.target.material_version_id));
  }, [caseId, selection]);

  useEffect(() => {
    let active = true;
    void Promise.all([
      listRevisionProposals(caseId),
      selection ? getWorkingDraft(caseId, selection.target.material_version_id) : Promise.resolve(null),
    ]).then(([items, nextDraft]) => {
      if (!active) return;
      setProposals(items);
      setDraft(nextDraft);
      setError(null);
    }).catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : String(reason)); });
    return () => { active = false; };
  }, [caseId, selection]);

  const current = selection ? [...proposals].reverse().find((item) =>
    item.issue_id === selection.issue.id &&
    item.source_material_version_id === selection.target.material_version_id &&
    item.target_quote === selection.target.quote,
  ) : undefined;

  useEffect(() => { setEditedText(current?.proposed_text ?? ''); setNote(current?.decision_note ?? ''); }, [current?.id, current?.proposed_text, current?.decision_note]);

  const generate = async (): Promise<void> => {
    if (!selection) return;
    setBusy(true); setError(null);
    try {
      await generateRevisionProposal(caseId, selection.issue.id, {
        material_version_id: selection.target.material_version_id,
        start_offset: selection.target.start_offset,
        end_offset: selection.target.end_offset,
      });
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); await refresh(); }
    finally { setBusy(false); }
  };

  const decide = async (decision: 'accepted' | 'rejected'): Promise<void> => {
    if (!current) return;
    setBusy(true); setError(null);
    try {
      await decideRevisionProposal(current.id, decision, current.version, decision === 'accepted' ? editedText : undefined, note);
      await refresh();
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); await refresh(); }
    finally { setBusy(false); }
  };

  const highlightAt = selection && draft && draft.text.split(selection.target.quote).length === 2
    ? draft.text.indexOf(selection.target.quote) : -1;
  const pendingCount = proposals.filter((item) => item.status === 'pending').length;
  const acceptedCount = proposals.filter((item) => item.status === 'accepted').length;

  return <section className="report-section revision-workspace" id="report-revisions" aria-label="文书与修改">
    <header className="revision-workspace__head">
      <div><span className="revision-workspace__eyebrow">文书工作区</span><h2>文书与修改</h2><p>在冻结原文上定位问题，人工确认后形成独立的文本工作稿。</p></div>
      <div className="revision-workspace__counts"><span>{pendingCount} 项待处理</span><span>{acceptedCount} 项已接受</span></div>
    </header>
    {!selection ? <p className="revision-workspace__empty">在上方问题卡中选择一段已核实的材料原文，即可定位、标注并准备修改。</p> : null}
    {selection ? <div className="revision-workspace__context"><span>当前定位</span><strong>{selection.target.logical_name} v{selection.target.version_number}</strong><span>{selection.issue.title}</span></div> : null}
    {error ? <div className="revision-workspace__error" role="alert">{error}</div> : null}
    {selection && draft ? <>
      <div className="revision-workspace__toolbar"><div><strong>文本工作稿 v{draft.version}</strong><span>原件始终保留；此处展示可复核的文本版本</span></div><div className="revision-workspace__view-switch" role="group" aria-label="查看方式"><button type="button" className={view === 'annotated' ? 'is-active' : ''} onClick={() => setView('annotated')}>标注</button><button type="button" className={view === 'reading' ? 'is-active' : ''} onClick={() => setView('reading')}>阅读</button></div></div>
      {view === 'annotated' ? <div className="revision-workspace__paper">
        <div className="revision-workspace__paper-meta">{selection.target.filename} · 需注意段落已标注</div>
        {highlightAt >= 0 ? <div className="revision-workspace__manuscript">
          <div className="revision-workspace__before">{draft.text.slice(0, highlightAt)}</div>
          <aside className="revision-workspace__annotation"><span>批注 {selection.issue.kind === 'missing_information' ? '· 待补充' : '· 建议修改'}</span><strong>{selection.issue.title}</strong><p>{current?.rationale ?? selection.issue.finding}</p></aside>
          <mark className="revision-workspace__highlight">{selection.target.quote}</mark>
          <div>{draft.text.slice(highlightAt + selection.target.quote.length)}</div>
        </div> : <div className="revision-workspace__changed"><strong>该段原文已在工作稿中改变</strong><p>请在阅读视图查看当前完整文本。若要继续修改同一段，请重新审查并定位。</p></div>}
      </div> : <div className="revision-workspace__paper revision-workspace__paper--reading"><MarkdownText variant="report" allowRawHtml={false}>{draft.text}</MarkdownText></div>}
    </> : null}
    {selection ? <div className="revision-workspace__proposal">
      <div className="revision-workspace__proposal-head"><div><span>修改提案</span><h3>{current ? current.status === 'pending' ? '等待人工确认' : current.status === 'accepted' ? '已形成工作稿' : current.status === 'rejected' ? '已驳回' : '审查结果已更新' : '尚未生成'}</h3></div>{canManageActions && (!current || current.status !== 'pending') ? <button type="button" className="revision-workspace__generate" disabled={busy || highlightAt < 0} onClick={() => void generate()}>{busy ? '正在准备…' : '生成修改提案'}</button> : null}</div>
      {current ? <><div className="revision-workspace__comparison"><div><span>原文</span><p>{current.target_quote}</p></div><div><span>{current.status === 'accepted' ? '实际采用文本' : '建议修改'}</span>{current.status === 'pending' && canManageActions ? <textarea aria-label="编辑建议文本" value={editedText} onChange={(event) => setEditedText(event.target.value)} /> : <p>{current.status === 'accepted' ? current.accepted_text : current.proposed_text}</p>}</div></div><div className="revision-workspace__reason"><span>修改理由</span><p>{current.rationale}</p>{current.open_points.length > 0 ? <div><strong>仍需确认</strong><ul>{current.open_points.map((point, index) => <li key={index}>{point}</li>)}</ul></div> : null}{current.citation_refs.length > 0 ? <small>法律依据：{current.citation_refs.join(' · ')}</small> : null}</div>{current.status === 'pending' && canManageActions ? <div className="revision-workspace__decision"><input aria-label="处理备注" placeholder="处理备注（可选）" value={note} onChange={(event) => setNote(event.target.value)} /><button type="button" disabled={busy} onClick={() => void decide('rejected')}>驳回</button><button type="button" className="revision-workspace__accept" disabled={busy || !editedText.trim()} onClick={() => void decide('accepted')}>{editedText !== current.proposed_text ? '接受编辑后的版本' : '接受修改'}</button></div> : null}</> : <p className="revision-workspace__empty">从当前问题的材料原文出发，由 Agent 准备具体的替换文字及理由。</p>}
    </div> : null}
  </section>;
}

