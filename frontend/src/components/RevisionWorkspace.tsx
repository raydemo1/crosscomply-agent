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
  /** Rendered inside the document side panel instead of as a standalone report section. */
  embedded?: boolean;
  /** Reports whether the editor holds text the user has not submitted yet. */
  onDirtyChange?: (dirty: boolean) => void;
  /** Closes the editor panel, returning the side column to the current annotation. */
  onClose?: () => void;
}

export default function RevisionWorkspace({ caseId, selection, canManageActions, embedded = false, onDirtyChange, onClose }: RevisionWorkspaceProps): JSX.Element | null {
  const [proposals, setProposals] = useState<RevisionProposalApi[]>([]);
  const [draft, setDraft] = useState<WorkingDraftApi | null>(null);
  const [editedText, setEditedText] = useState('');
  const [note, setNote] = useState('');
  const [baseline, setBaseline] = useState({ text: '', note: '' });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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

  useEffect(() => {
    const text = current?.proposed_text ?? '';
    const currentNote = current?.decision_note ?? '';
    setEditedText(text);
    setNote(currentNote);
    setBaseline({ text, note: currentNote });
  }, [current?.id, current?.proposed_text, current?.decision_note]);

  const dirty = Boolean(current && current.status === 'pending' && canManageActions
    && (editedText !== baseline.text || note !== baseline.note));
  useEffect(() => { onDirtyChange?.(dirty); }, [dirty, onDirtyChange]);

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

  const highlightAt = selection && draft
    ? draft.version === 0 && draft.text.slice(selection.target.start_offset, selection.target.end_offset) === selection.target.quote
      ? selection.target.start_offset
      : draft.text.split(selection.target.quote).length === 2 ? draft.text.indexOf(selection.target.quote) : -1
    : -1;
  return <section className={'report-section revision-workspace' + (embedded ? ' revision-workspace--embedded' : '')} aria-label="文书修改">
    <header className="revision-workspace__head">
      <div><h2>文书修改</h2></div>
      {onClose ? <button type="button" className="revision-workspace__close" aria-label="关闭文书修改" onClick={onClose}>×</button> : null}
    </header>
    {error ? <div className="revision-workspace__error" role="alert">{error}</div> : null}
    {selection ? <div className="revision-workspace__proposal">
      <div className="revision-workspace__proposal-head"><div><span>{selection.target.logical_name} v{selection.target.version_number} · {current?.status === 'accepted' ? '已形成工作稿' : current?.status === 'pending' ? '待人工确认' : '待准备修改'}</span></div>{canManageActions && (!current || current.status !== 'pending') ? <button type="button" className="revision-workspace__generate" disabled={busy || highlightAt < 0} onClick={() => void generate()}>{busy ? '正在准备…' : '生成修改建议'}</button> : null}</div>
      <div className="revision-workspace__comparison"><div><span>原文</span><p>{selection.target.quote}</p></div><div><span>{current?.status === 'accepted' ? '实际采用文本' : '建议文本'}</span>{current?.status === 'pending' && canManageActions ? <textarea aria-label="编辑建议文本" value={editedText} onChange={(event) => setEditedText(event.target.value)} /> : <p>{current ? current.status === 'accepted' ? current.accepted_text : current.proposed_text : '生成后在这里核对建议文本。'}</p>}</div></div>
      {current ? <><details className="revision-workspace__reason"><summary>查看修改理由与适用边界</summary><p>{current.rationale}</p>{current.open_points.length > 0 ? <div><strong>仍需确认</strong><ul>{current.open_points.map((point, index) => <li key={index}>{point}</li>)}</ul></div> : null}{current.citation_refs.length > 0 ? <small>法律依据：{current.citation_refs.join(' · ')}</small> : null}</details>{current.status === 'pending' && canManageActions ? <div className="revision-workspace__decision"><small className="revision-workspace__reminder">接受后形成独立工作稿，原件保持不变。</small><input aria-label="处理备注" placeholder="处理备注（可选）" value={note} onChange={(event) => setNote(event.target.value)} /><button type="button" disabled={busy} onClick={() => void decide('rejected')}>驳回</button><button type="button" className="revision-workspace__accept" disabled={busy || !editedText.trim()} onClick={() => void decide('accepted')}>{editedText !== current.proposed_text ? '接受编辑后的版本' : '接受修改'}</button></div> : null}</> : null}
    </div> : null}
    {draft ? <details className="revision-workspace__full-draft"><summary>阅读完整工作稿 · v{draft.version}</summary><div className="revision-workspace__paper revision-workspace__paper--reading"><MarkdownText variant="report" allowRawHtml={false}>{draft.text}</MarkdownText></div></details> : null}
  </section>;
}