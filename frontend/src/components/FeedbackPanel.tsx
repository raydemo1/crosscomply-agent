import { useEffect, useId, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ApiError, createEvalDraft, listEvalSignals } from '../api/client';
import type { SavedCase } from '../types/case';
import type { UserRole } from '../types/api';
import { setConclusionUseful, setFeedbackText } from '../store/caseStore';

interface FeedbackPanelProps {
  saved: SavedCase;
  viewerRole: UserRole;
}

export default function FeedbackPanel({ saved, viewerRole }: FeedbackPanelProps): JSX.Element {
  const fieldId = useId();
  const navigate = useNavigate();
  const feedback = saved.feedback;
  const [missingDraft, setMissingDraft] = useState(feedback?.missingSources ?? '');
  const [notesDraft, setNotesDraft] = useState(feedback?.notes ?? '');
  const conclusionUseful = feedback?.conclusionUseful ?? null;
  const [candidate, setCandidate] = useState(false);
  const [creating, setCreating] = useState(false);
  const [draftError, setDraftError] = useState('');

  useEffect(() => {
    setMissingDraft(feedback?.missingSources ?? '');
    setNotesDraft(feedback?.notes ?? '');
  }, [saved.id, feedback?.missingSources, feedback?.notes]);

  useEffect(() => {
    let active = true;
    if (viewerRole !== 'requester' && saved.reviewTask?.status === 'succeeded') {
      void listEvalSignals(saved.id).then(({ items }) => { if (active) setCandidate(items.length > 0); })
        .catch(() => { if (active) setCandidate(false); });
    }
    return () => { active = false; };
  }, [saved.id, saved.reviewTask?.status, saved.signedDecision?.status, feedback?.updatedAt, viewerRole]);

  async function createDraft() {
    setCreating(true);
    setDraftError('');
    try {
      const draft = await createEvalDraft(saved.id);
      navigate(`/governance?tab=quality&draft=${draft.id}`);
    } catch (reason) {
      const existing = reason instanceof ApiError && reason.status === 409 ? (reason.detail as { draft_id?: string } | null)?.draft_id : null;
      if (existing) navigate(`/governance?tab=quality&draft=${existing}`);
      else setDraftError(reason instanceof Error ? reason.message : '无法创建评测草稿');
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="feedback">
      <div className="feedback__block">
        <div className="feedback__label">{viewerRole === 'requester' ? '这份报告是否回答了你的问题' : '结论是否有助于复核'}</div>
        <div className="feedback__toggle-group">
          <button type="button" aria-pressed={conclusionUseful === true} className={'feedback-toggle' + (conclusionUseful === true ? ' is-active is-useful' : '')} onClick={() => setConclusionUseful(saved.id, conclusionUseful === true ? null : true)}>
            <span aria-hidden="true">✓</span> 有用
          </button>
          <button type="button" aria-pressed={conclusionUseful === false} className={'feedback-toggle' + (conclusionUseful === false ? ' is-active is-useless' : '')} onClick={() => setConclusionUseful(saved.id, conclusionUseful === false ? null : false)}>
            <span aria-hidden="true">×</span> 需要修订
          </button>
        </div>
      </div>
      <div className="feedback__block">
        <label className="feedback__label" htmlFor={`${fieldId}-sources`}>{viewerRole === 'requester' ? '需要补充核查的材料或来源' : '缺少来源'}</label>
        <textarea id={`${fieldId}-sources`} className="feedback__textarea" placeholder="描述本次审查缺少的法规或事实来源" value={missingDraft} onChange={(event) => setMissingDraft(event.target.value)} onBlur={() => setFeedbackText(saved.id, 'missingSources', missingDraft)} rows={2} />
      </div>
      <div className="feedback__block">
        <label className="feedback__label" htmlFor={`${fieldId}-notes`}>{viewerRole === 'requester' ? '审查问题反馈' : '复核意见'}</label>
        <textarea id={`${fieldId}-notes`} className="feedback__textarea" placeholder={viewerRole === 'requester' ? '说明你发现的事实或引用问题' : '记录人工判断、例外情况或后续复核意见'} value={notesDraft} onChange={(event) => setNotesDraft(event.target.value)} onBlur={() => setFeedbackText(saved.id, 'notes', notesDraft)} rows={3} />
      </div>
      {viewerRole !== 'requester' && saved.feedbackEntries.some((entry) => entry.actor_role === 'requester' && (entry.notes || entry.missing_sources)) ? <div className="feedback__block">
        <div className="feedback__label">申报人反馈</div>
        {saved.feedbackEntries.filter((entry) => entry.actor_role === 'requester' && (entry.notes || entry.missing_sources)).map((entry) => <div className="feedback__entry" key={entry.actor_id}>
          <strong>{entry.actor_name}</strong>
          {entry.notes ? <p>{entry.notes}</p> : null}
          {entry.missing_sources ? <p>建议核查：{entry.missing_sources}</p> : null}
        </div>)}
      </div> : null}
      {feedback?.updatedAt ? <div className="feedback__updated">已于 {feedback.updatedAt.replace('T', ' ').slice(0, 16)} 更新</div> : null}
      {candidate && viewerRole !== 'requester' && saved.reviewTask?.status === 'succeeded' ? <div className="feedback__block">
        <button type="button" className="btn-secondary" disabled={creating} onClick={() => void createDraft()}>转为评测用例</button>
        {draftError ? <p className="ops-error" role="alert">{draftError}</p> : null}
      </div> : null}
    </div>
  );
}
