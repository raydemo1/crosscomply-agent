import { useState } from 'react';
import type { SavedCase } from '../types/case';
import { setConclusionUseful, setFeedbackText } from '../store/caseStore';

interface FeedbackPanelProps {
  saved: SavedCase;
}

export default function FeedbackPanel({ saved }: FeedbackPanelProps): JSX.Element {
  const feedback = saved.feedback;
  const [missingDraft, setMissingDraft] = useState(feedback?.missingSources ?? '');
  const [notesDraft, setNotesDraft] = useState(feedback?.notes ?? '');
  const conclusionUseful = feedback?.conclusionUseful ?? null;

  return (
    <div className="feedback">
      <div className="feedback__block">
        <div className="feedback__label">结论是否有助于决策</div>
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
        <div className="feedback__label">缺少来源</div>
        <textarea className="feedback__textarea" placeholder="描述本次审查缺少的法规或事实来源" value={missingDraft} onChange={(event) => setMissingDraft(event.target.value)} onBlur={() => setFeedbackText(saved.id, 'missingSources', missingDraft)} rows={2} />
      </div>
      <div className="feedback__block">
        <div className="feedback__label">审核备注</div>
        <textarea className="feedback__textarea" placeholder="记录人工判断、例外情况或后续复核意见" value={notesDraft} onChange={(event) => setNotesDraft(event.target.value)} onBlur={() => setFeedbackText(saved.id, 'notes', notesDraft)} rows={3} />
      </div>
      {feedback?.updatedAt ? <div className="feedback__updated">已于 {feedback.updatedAt.replace('T', ' ').slice(0, 16)} 更新</div> : null}
    </div>
  );
}
