import { useEffect, useState } from 'react';
import type { SavedCase } from '../types/case';
import type { UserRole } from '../types/api';
import { setConclusionUseful, setFeedbackText } from '../store/caseStore';

interface FeedbackPanelProps {
  saved: SavedCase;
  viewerRole: UserRole;
}

export default function FeedbackPanel({ saved, viewerRole }: FeedbackPanelProps): JSX.Element {
  const feedback = saved.feedback;
  const [missingDraft, setMissingDraft] = useState(feedback?.missingSources ?? '');
  const [notesDraft, setNotesDraft] = useState(feedback?.notes ?? '');
  const conclusionUseful = feedback?.conclusionUseful ?? null;

  useEffect(() => {
    setMissingDraft(feedback?.missingSources ?? '');
    setNotesDraft(feedback?.notes ?? '');
  }, [saved.id, feedback?.missingSources, feedback?.notes]);

  return (
    <div className="feedback">
      <div className="feedback__block">
        <div className="feedback__label">{viewerRole === 'requester' ? '这份报告是否回答了你的问题' : '结论是否有助于复核'}</div>
        <div className="feedback__toggle-group">
          <button type="button" className={'feedback-toggle' + (conclusionUseful === true ? ' is-active is-useful' : '')} onClick={() => setConclusionUseful(saved.id, conclusionUseful === true ? null : true)}>
            <span aria-hidden="true">✓</span> 有用
          </button>
          <button type="button" className={'feedback-toggle' + (conclusionUseful === false ? ' is-active is-useless' : '')} onClick={() => setConclusionUseful(saved.id, conclusionUseful === false ? null : false)}>
            <span aria-hidden="true">×</span> 需要修订
          </button>
        </div>
      </div>
      <div className="feedback__block">
        <div className="feedback__label">{viewerRole === 'requester' ? '需要补充核查的材料或来源' : '缺少来源'}</div>
        <textarea className="feedback__textarea" placeholder="描述本次审查缺少的法规或事实来源" value={missingDraft} onChange={(event) => setMissingDraft(event.target.value)} onBlur={() => setFeedbackText(saved.id, 'missingSources', missingDraft)} rows={2} />
      </div>
      <div className="feedback__block">
        <div className="feedback__label">{viewerRole === 'requester' ? '审查问题反馈' : '复核意见'}</div>
        <textarea className="feedback__textarea" placeholder={viewerRole === 'requester' ? '说明你发现的事实或引用问题' : '记录人工判断、例外情况或后续复核意见'} value={notesDraft} onChange={(event) => setNotesDraft(event.target.value)} onBlur={() => setFeedbackText(saved.id, 'notes', notesDraft)} rows={3} />
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
    </div>
  );
}
