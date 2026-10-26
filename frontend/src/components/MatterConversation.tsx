import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { MessageCircle, ArrowUp, ArrowDown, LoaderCircle, X, Quote, RotateCcw } from 'lucide-react';
import type { MatterSelection, UserRole } from '../types/api';
import type { SavedCase } from '../types/case';
import { useMatterConversation } from '../hooks/useMatterConversation';
import { useCaseAssistant } from '../hooks/useCaseAssistant';
import { factLabel, factValue, valuesChanged } from '../utils/factFields';
import MarkdownText from './MarkdownText';

export function PassageQuote({ selection, onRemove }: { selection: MatterSelection; onRemove?: () => void }) {
  return <div className="matter-quote"><div><Quote size={14} aria-hidden="true" /><span>{selection.label || (selection.kind === 'material' ? '材料原文' : '审查报告')}</span>{onRemove ? <button type="button" onClick={onRemove} aria-label="移除引用"><X size={14} /></button> : null}</div><p>{selection.quote}</p></div>;
}

export default function MatterConversation({ saved, viewerRole }: { saved: SavedCase; viewerRole: UserRole }) {
  const { active, selection, setSelection, close } = useCaseAssistant();
  const opened = active === 'conversation';
  const { turns, busy, loading, error, streaming, ask, retryStreaming, confirm } = useMatterConversation(saved);
  const [question, setQuestion] = useState('');
  const [latest, setLatest] = useState(false);
  const [modal, setModal] = useState(() => !window.matchMedia('(min-width: 1640px)').matches);
  const historyRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLElement>(null);
  const nearBottom = useRef(true);
  const task = saved.reviewTask!;
  const citations = saved.response && !('failed_node' in saved.response) ? saved.response.evidence_chunks : [];
  const toLatest = () => {
    const history = historyRef.current;
    if (history) history.scrollTop = history.scrollHeight;
    nearBottom.current = true;
    setLatest(false);
  };
  useEffect(() => {
    if (!opened) return;
    const frame = requestAnimationFrame(() => { if (nearBottom.current) toLatest(); });
    return () => cancelAnimationFrame(frame);
  }, [opened, turns.length, streaming?.text, streaming?.error, streaming?.status, busy]);
  useEffect(() => {
    if (opened && !nearBottom.current) setLatest(true);
  }, [opened, streaming?.text, turns.length]);
  useLayoutEffect(() => {
    if (!opened) return;
    const media = window.matchMedia('(min-width: 1640px)');
    const main = panelRef.current?.parentElement?.querySelector<HTMLElement>('.case-assistant-main');
    const previousOverflow = document.body.style.overflow;
    const update = () => {
      setModal(!media.matches);
      if (main) main.inert = !media.matches;
      document.body.style.overflow = media.matches ? previousOverflow : 'hidden';
    };
    update();
    media.addEventListener('change', update);
    return () => { if (main) main.inert = false; document.body.style.overflow = previousOverflow; media.removeEventListener('change', update); };
  }, [opened]);
  const submit = () => {
    const text = question.trim();
    if (!text || busy || loading) return;
    setQuestion('');
    nearBottom.current = true;
    setLatest(false);
    void ask(text, selection);
  };
  return <aside id="case-conversation-panel" className="matter-panel" ref={panelRef} hidden={!opened} role="dialog" aria-modal={modal || undefined} aria-label="向 CrossComply 提问" aria-describedby="matter-scope" onKeyDown={(event) => {
    if (event.key !== 'Tab' || window.matchMedia('(min-width: 1640px)').matches) return;
    const items = panelRef.current?.querySelectorAll<HTMLElement>('button:not(:disabled), textarea:not(:disabled), a[href], summary');
    if (!items?.length) return;
    const first = items[0]; const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  }}>
    <header className="matter-panel__header"><div className="matter-panel__identity"><span><MessageCircle size={19} strokeWidth={1.8} aria-hidden="true" /></span><div><h2>CrossComply</h2><p>围绕本案，进一步了解</p></div></div><button type="button" className="matter-icon-button" aria-label="收起案件问答" onClick={() => close('conversation')}><X size={20} /></button></header>
    <div className="matter-panel__history" ref={historyRef} role="region" aria-label="当前案件的问答记录" onScroll={() => {
      const history = historyRef.current;
      if (!history) return;
      nearBottom.current = history.scrollHeight - history.scrollTop - history.clientHeight < 64;
      if (nearBottom.current) setLatest(false);
    }}>
      {loading ? <p className="matter-panel__status" role="status">正在读取本案对话…</p> : null}
      {!loading && !turns.length && !streaming ? <div className="matter-panel__empty"><MessageCircle size={27} strokeWidth={1.4} aria-hidden="true" /><h3>{selection ? '从这段内容继续' : '把本案的问题聊清楚'}</h3><p>{selection ? '解释含义、核对依据，或讨论它对本案的影响。' : '选中报告或材料中的文字，带着上下文追问；也可以直接提出问题。'}</p></div> : null}
      {turns.map((turn) => <section className="matter-panel__turn" key={turn.id}>
        <div className="matter-panel__question">{turn.selection ? <PassageQuote selection={turn.selection} /> : null}<p>{turn.question}</p></div>
        <span className="matter-panel__answer-label">CrossComply</span>
        <MarkdownText allowRawHtml={false} variant="note">{turn.reply.answer}</MarkdownText>
        {turn.reply.claims.length ? <details className="matter-panel__sources"><summary>法律依据 · {turn.reply.claims.length}</summary>{turn.reply.claims.map((claim, i) => <div key={i}><p>{claim.text}</p>{claim.supporting_chunk_ids.map((id) => { const source = citations?.find((c) => c.chunk_id === id); return source ? <a key={id} href={source.source_url} target="_blank" rel="noopener noreferrer">{source.citation_label || source.title}</a> : <span key={id}>依据见本案正式报告</span>; })}</div>)}</details> : null}
        {turn.reply.material_citations.length ? <details className="matter-panel__sources"><summary>材料原文 · {turn.reply.material_citations.length}</summary>{turn.reply.material_citations.map((citation, i) => <div key={i}><small>{citation.filename || '本案冻结材料'}</small><blockquote>{citation.quote}</blockquote></div>)}</details> : null}
        {turn.reply.fact_fields.length ? <p className="matter-panel__fact-fields">涉及事实：{turn.reply.fact_fields.map(factLabel).join('、')}</p> : null}
        {Object.keys(turn.reply.proposed_facts).length ? <section className="matter-panel__proposals"><strong>待核对的补充事实</strong><p>请核对具体数值和统计口径，确认后才会用于重新审查。</p><dl className="case-facts__values">{Object.entries(turn.reply.proposed_facts).map(([field, value]) => <div key={field}><dt>{factLabel(field)}</dt><dd>{factValue(field, value)}</dd></div>)}</dl>{viewerRole === 'requester' && saved.status === 'needs_info' && task.status === 'succeeded' && valuesChanged(turn.reply.proposed_facts, saved.intakeSnapshot!.intake) ? <button className="btn-primary" type="button" disabled={busy} onClick={() => void confirm(turn)}>确认这些事实并重新审查</button> : <p>补充事实需要申请人明确确认。</p>}</section> : null}
      </section>)}
      {streaming ? <section className="matter-panel__turn" aria-busy={busy}>
        <div className="matter-panel__question">{streaming.selection ? <PassageQuote selection={streaming.selection} /> : null}<p>{streaming.question}</p></div>
        <span className="matter-panel__answer-label">CrossComply</span>
        {streaming.text ? <div className={busy ? 'matter-panel__streaming' : ''}><MarkdownText allowRawHtml={false} variant="note">{streaming.text}</MarkdownText></div> : busy ? <p className="matter-panel__status" role="status"><LoaderCircle size={15} aria-hidden="true" />{streaming.status}</p> : null}
        {streaming.error ? <div className="matter-panel__error" role="alert"><p>{streaming.error}</p><button type="button" disabled={busy} onClick={retryStreaming}><RotateCcw size={14} aria-hidden="true" />重新回答</button></div> : null}
      </section> : busy ? <p className="matter-panel__status" role="status">正在处理，请稍候…</p> : null}
      {error ? <p className="matter-panel__error" role="alert">{error}</p> : null}
    </div>
    <div className="matter-panel__bottom">
      {latest ? <button type="button" className="matter-panel__latest" onClick={toLatest}><ArrowDown size={14} aria-hidden="true" />回到最新</button> : null}
      {selection ? <PassageQuote selection={selection} onRemove={() => setSelection(undefined)} /> : null}
      {selection && !busy ? <div className="matter-panel__suggestions" aria-label="快捷问题">{['解释这段内容', '与本案结论有什么关系', '还需补充哪些信息'].map((text) => <button type="button" key={text} onClick={() => { setQuestion(text); document.getElementById('matter-question')?.focus(); }}>{text}</button>)}</div> : null}
      <form className="matter-panel__composer" onSubmit={(event) => { event.preventDefault(); submit(); }}>
        <label className="sr-only" htmlFor="matter-question">向 CrossComply 提问</label><textarea id="matter-question" value={question} maxLength={2000} rows={3} onChange={(event) => setQuestion(event.target.value)} placeholder={busy ? '可以先写下一个问题…' : '继续问问本案的结论、依据或材料…'} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); submit(); } }} />
        <div><span>Enter 发送 · Shift + Enter 换行</span><button type="submit" disabled={busy || loading || !question.trim()} aria-label="发送问题"><ArrowUp size={18} aria-hidden="true" /></button></div>
      </form>
      <p className="matter-panel__scope" id="matter-scope">基于本案材料与事实回答，不改变正式审查结果。</p>
    </div>
  </aside>;
}
