import { useEffect, useRef, useState } from 'react';
import { MessageCircle, ArrowUp, LoaderCircle, ChevronDown } from 'lucide-react';
import type { UserRole } from '../types/api';
import type { SavedCase } from '../types/case';
import { useMatterConversation } from '../hooks/useMatterConversation';
import { factLabel, factValue, valuesChanged } from '../utils/factFields';
import MarkdownText from './MarkdownText';
import './CaseFacts.css';

const QUESTIONS = ['为什么得出目前的判断？', '这条结论对应哪份材料？', '还缺什么才能送审？'];

export default function MatterConversation({ saved, viewerRole }: { saved: SavedCase; viewerRole: UserRole }) {
  const { turns, busy, loading, error, ask, confirm, key } = useMatterConversation(saved);
  const [question, setQuestion] = useState('');
  const [initiallyOpen] = useState(() => window.matchMedia('(min-width: 1180px)').matches);
  const historyRef = useRef<HTMLDivElement>(null);
  useEffect(() => { setQuestion(''); }, [key]);
  useEffect(() => {
    const history = historyRef.current;
    if (history) history.scrollTop = history.scrollHeight;
  }, [turns.length, busy]);
  const task = saved.reviewTask;
  if (!task || saved.materialSnapshot?.id !== task.material_snapshot_id || saved.intakeSnapshot?.id !== task.intake_snapshot_id) return null;
  const citations = saved.response && !('failed_node' in saved.response) ? saved.response.evidence_chunks : [];
  const submit = async () => { if (question.trim() && await ask(question.trim())) setQuestion(''); };
  return <details className="card matter-conversation" open={initiallyOpen}>
    <summary><MessageCircle size={20} aria-hidden="true" /><span><strong>案件问答</strong><small>解释结论 · 找到依据 · 了解缺口</small></span><ChevronDown className="case-disclosure-chevron" size={16} aria-hidden="true" /></summary>
    <div className="matter-conversation__body">
    <p className="matter-conversation__scope">基于本案材料与事实回答，不改变正式审查结果。</p>
    <div className="matter-conversation__history" ref={historyRef} role="region" aria-label="当前案件的问答记录" tabIndex={turns.length ? 0 : undefined}>
    {loading && <p role="status">正在读取当前案件对话…</p>}
    {!loading && !turns.length ? <div className="matter-conversation__empty"><span>对结论有疑问？</span><p>从下面的问题开始，或直接写下你想了解的内容。</p></div> : null}
    {turns.map((turn) => <section className="matter-conversation__turn" key={turn.id}>
      <h3><span>你问</span>{turn.question}</h3>
      <span className="matter-conversation__answer-label">Agent 回答</span>
      <MarkdownText allowRawHtml={false} variant="note">{turn.reply.answer}</MarkdownText>
      {!!turn.reply.claims.length && <details><summary>查看法律依据</summary>{turn.reply.claims.map((claim, i) => <div key={i}><p>{claim.text}</p>{claim.supporting_chunk_ids.map((id) => { const source = citations?.find((c) => c.chunk_id === id); return source ? <a key={id} href={source.source_url} target="_blank" rel="noopener noreferrer">{source.citation_label || source.title}</a> : <span key={id}>依据见本案正式报告</span>; })}</div>)}</details>}
      {!!turn.reply.material_citations.length && <details><summary>查看材料原文</summary>{turn.reply.material_citations.map((citation, i) => <div key={i}><small>{citation.filename || '本案冻结材料'}</small><blockquote>{citation.quote}</blockquote></div>)}</details>}
      {!!turn.reply.fact_fields.length && <p>涉及事实：{turn.reply.fact_fields.map(factLabel).join('、')}</p>}
      {!!Object.keys(turn.reply.proposed_facts).length && <section>
        <strong>待核对的补充事实</strong><p>这些值只是对话中整理的建议，尚未正式确认。请核对具体数值和统计口径。</p>
        <dl className="case-facts__values">{Object.entries(turn.reply.proposed_facts).map(([field, value]) => <div key={field}><dt>{factLabel(field)}</dt><dd>{factValue(field, value)}</dd></div>)}</dl>
        {viewerRole === 'requester' && saved.status === 'needs_info' && task.status === 'succeeded' && valuesChanged(turn.reply.proposed_facts, saved.intakeSnapshot!.intake) ? <button className="btn-primary" type="button" disabled={busy} onClick={() => void confirm(turn)}>确认这些事实并重新审查</button> : <p>补充事实需要申请人明确确认；审批中的案件不能在这里更改事实。</p>}
      </section>}
    </section>)}
    {busy ? <p className="matter-conversation__busy" role="status"><LoaderCircle className="case-busy-icon" size={16} aria-hidden="true" />正在处理，请稍候…</p> : null}
    </div>
    <div className="matter-conversation__suggestions">{QUESTIONS.map((q) => <button type="button" className="case-header__action-btn" key={q} disabled={busy} onClick={() => setQuestion(q)}>{q}</button>)}</div>
    <form className="matter-conversation__composer" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
    <label htmlFor="matter-question">你的问题</label><textarea id="matter-question" value={question} maxLength={2000} rows={3} disabled={busy} onChange={(e) => setQuestion(e.target.value)} placeholder="也可以问：如果接收方改成日本，会有什么影响？" />
    <div><small>仅针对当前案件</small><button type="submit" className="btn-primary" disabled={busy || loading || !question.trim()}><ArrowUp size={16} aria-hidden="true" />询问 Agent</button></div>
    </form>
    {error && <p className="matter-conversation__error" role="alert">{error}</p>}
    <details className="matter-conversation__help"><summary>回答基于哪些内容？</summary><p>本次审查冻结的材料、事实来源、正式报告及已核验证据。对话不会自行确认事实或改变审批状态。</p></details>
    </div>
  </details>;
}
