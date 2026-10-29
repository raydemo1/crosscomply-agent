import { useEffect, useRef, useState } from 'react';
import { ArrowRight, ArrowUp, Check, TriangleAlert, CircleHelp, FileText, Paperclip, Plus, X } from 'lucide-react';
import { createQAConversation, downloadQAMaterial, getQAConversation, retryQARun, sendQAMessage, subscribeQARun, uploadQAMaterial, validateUploadFile } from '../api/client';
import type { QAConversation, QAHandoff, QAQuestion, QARun } from '../types/qa';
import QAAnswer from './QAAnswer';
import { formatQAAnswers, qaQuestionOptions, toggleQAOption } from '../utils/qaQuestions';
import '../styles/qa.css';

interface Props {
  userRole: 'requester' | 'reviewer' | 'admin';
  conversationId: string | null;
  onConversationChange: (id: string) => void;
  onHistoryChange: () => void;
  onNewConversation: () => void;
  onHandoff: (handoff: QAHandoff) => void;
}

const EXAMPLES = ['数据出境有哪些常见合规路径？', '员工信息出境需要满足什么条件？', '上海自贸区负面清单应该如何适用？'];

function QAQuestions({ questions, disabled, onSubmit }: {
  questions: QAQuestion[]; disabled: boolean; onSubmit: (answer: string) => Promise<void>;
}): JSX.Element {
  const [answers, setAnswers] = useState<Record<number, string[]>>({});
  const [custom, setCustom] = useState<Record<number, string>>({});
  const formatted = formatQAAnswers(questions, answers, custom);
  return <form className="qa-questions" onSubmit={(event) => {
    event.preventDefault();
    if (!formatted || disabled) return;
    void onSubmit(formatted);
  }}>
    {questions.map((item, i) => <fieldset key={i} disabled={disabled}>
      <legend>{item.question}{item.selection === 'multiple' ? <span>可多选</span> : null}</legend>
      <div className="qa-question-options">{qaQuestionOptions(item.options).map((option, j) => {
        const checked = (answers[i] ?? []).includes(option);
        return <label key={j} className={checked ? 'is-selected' : ''}>
          <input type={item.selection === 'single' ? 'radio' : 'checkbox'} name={`question-${i}`} checked={checked} onChange={() => {
            setAnswers((prior) => {
              return { ...prior, [i]: toggleQAOption(prior[i] ?? [], option, item.selection) };
            });
          }} /><Check size={12} aria-hidden="true" />{option}
        </label>;
      })}</div>
      <details className="qa-question-custom">
        <summary>补充说明</summary>
        <textarea aria-label={`${item.question} 补充说明`} rows={1} maxLength={1500} value={custom[i] ?? ''} onChange={(event) => setCustom((prior) => ({ ...prior, [i]: event.target.value }))} placeholder="填写实际情况，也可以直接在下方回复…" />
      </details>
    </fieldset>)}
    <div className="qa-question-actions"><button type="submit" className="qa-question-send" disabled={!formatted || disabled}>发送<ArrowUp size={14} /></button></div>
  </form>;
}

import QaOnboarding from './QaOnboarding';

export default function ComplianceQAPage({ userRole, conversationId, onConversationChange, onHistoryChange, onNewConversation, onHandoff }: Props): JSX.Element {
  const [conversation, setConversation] = useState<QAConversation | null>(null);
  const [question, setQuestion] = useState('');
  const [files, setFiles] = useState<File[]>([]);
  const [selected, setSelected] = useState<string[]>([]);
  const [sending, setSending] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [disconnected, setDisconnected] = useState(false);
  const [handoffRun, setHandoffRun] = useState<QARun | null>(null);
  const [draggingFiles, setDraggingFiles] = useState(false);
  const [showGuide, setShowGuide] = useState(false);
  const dragDepthRef = useRef(0);
  const [handoffMaterials, setHandoffMaterials] = useState<string[]>([]);
  const [preparing, setPreparing] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const questionInput = useRef<HTMLTextAreaElement>(null);
  const end = useRef<HTMLDivElement>(null);
  const sendKey = useRef<{ text: string; id: string } | null>(null);
  const onHistory = useRef(onHistoryChange);
  onHistory.current = onHistoryChange;
  const active = conversation?.runs.find((run) => run.status === 'queued' || run.status === 'running');
  const hasMessages = Boolean(conversation?.runs.length);
  const busy = sending || Boolean(active) || loading;

  useEffect(() => {
    let cancelled = false;
    setError(null);
    setHandoffRun(null);
    setConversation(null);
    setSelected([]);
    setFiles([]);
    setQuestion('');
    if (!conversationId) return;
    setLoading(true);
    void getQAConversation(conversationId).then((row) => {
      if (cancelled) return;
      setConversation(row);
      setSelected(row.runs[row.runs.length - 1]?.material_ids ?? row.materials.map((m) => m.id));
    }).catch((reason: unknown) => {
      if (!cancelled) setError(reason instanceof Error ? reason.message : '无法打开问答');
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [conversationId]);

  useEffect(() => {
    if (!conversation || !active) return;
    setDisconnected(false);
    return subscribeQARun(conversation.id, active.id, (run) => {
      setDisconnected(false);
      setConversation((row) => row ? { ...row, runs: row.runs.map((item) => item.id === run.id ? run : item) } : row);
      if (run.status === 'completed' || run.status === 'failed') onHistory.current();
    }, () => setDisconnected(true));
  }, [conversation?.id, active?.id]);

  useEffect(() => {
    if (hasMessages && end.current) {
      const rect = end.current.getBoundingClientRect();
      if (rect.top < window.innerHeight + 240) end.current.scrollIntoView({ block: 'nearest' });
    }
  }, [conversation?.runs[conversation.runs.length - 1]?.answer, hasMessages]);

  const send = async (text = question): Promise<void> => {
    if (!text.trim() || busy) return;
    setSending(true);
    setError(null);
    try {
      const row = conversation ?? await createQAConversation();
      setConversation(row);
      const materialIds = [...selected];
      const added = [];
      for (const file of files) {
        const material = await uploadQAMaterial(row.id, file);
        added.push(material);
        materialIds.push(material.id);
        setConversation({ ...row, materials: [...row.materials, ...added] });
        setSelected([...materialIds]);
        setFiles((items) => items.filter((item) => item !== file));
      }
      const signature = JSON.stringify([text.trim(), materialIds]);
      if (sendKey.current?.text !== signature) sendKey.current = { text: signature, id: crypto.randomUUID() };
      const run = await sendQAMessage(row.id, text.trim(), materialIds, sendKey.current.id);
      setConversation({ ...row, materials: [...row.materials, ...added], runs: [...row.runs.filter((r) => r.id !== run.id), run] });
      setSelected(materialIds);
      setQuestion('');
      setFiles([]);
      sendKey.current = null;
      if (row.id !== conversationId) onConversationChange(row.id);
      onHistory.current();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '发送失败，请重试');
    } finally {
      setSending(false);
    }
  };

  const retry = async (run: QARun): Promise<void> => {
    if (!conversation || busy) return;
    setError(null);
    try {
      const next = await retryQARun(conversation.id, run.id);
      setConversation({ ...conversation, runs: conversation.runs.map((r) => r.id === next.id ? next : r) });
    } catch (reason) { setError(reason instanceof Error ? reason.message : '重试失败'); }
  };

  const prepareHandoff = async (): Promise<void> => {
    if (!conversation || !handoffRun) return;
    setPreparing(true);
    setError(null);
    try {
      const selectedMaterials = conversation.materials.filter((m) => handoffMaterials.includes(m.id));
      const originals = await Promise.all(selectedMaterials.map((m) => downloadQAMaterial(conversation.id, m)));
      onHandoff({ conversationId: conversation.id, runId: handoffRun.id, question: handoffRun.question,
        files: originals, materialIds: selectedMaterials.map((m) => m.id),
        leads: handoffRun.answer, requestId: crypto.randomUUID() });
    } catch (reason) { setError(reason instanceof Error ? reason.message : '无法准备材料'); }
    finally { setPreparing(false); }
  };

  return (
    <section className={'qa-page' + (hasMessages ? ' qa-page--conversation' : '')}>
      {hasMessages ? <header className="qa-thread-heading"><div><h1>{conversation?.title}</h1></div><button className="qa-quiet-button" type="button" onClick={onNewConversation}><Plus size={16} />新问答</button></header> : (
        <header className="qa-welcome">
          <h1>咨询数据出境<br /><span>合规问题</span></h1>
        </header>
      )}

      {loading ? <p className="qa-loading" role="status">正在恢复对话…</p> : null}
      <div className="qa-messages" aria-label="问答记录">
        {conversation?.runs.map((run, index) => (
          <article className="qa-turn" key={run.id}>
            <div className="qa-user-message"><span className="qa-message-label">你的问题</span><p>{run.question}</p>{run.material_ids.length ? <div className="qa-attached-labels">{conversation.materials.filter((m) => run.material_ids.includes(m.id)).map((m) => <span key={m.id}><Paperclip size={13} />{m.filename}</span>)}</div> : null}</div>
            <div className="qa-answer">
              <div className="qa-answer-brand"><img src="/crosscomply-logo.svg" alt="" /><span>CrossComply</span><span className="qa-answer-kind">合规问答</span></div>
              {run.status === 'queued' || run.status === 'running' ? <div className="qa-progress" role="status"><span className="qa-progress-dot" />{run.progress}{disconnected ? ' · 连接恢复中' : ''}</div> : null}
              <QAAnswer answer={run.result?.questions?.length ? '' : run.answer} result={run.result} streaming={run.status === 'running'} incomplete={run.status === 'failed'}>
              {run.status === 'failed' ? <div className="qa-failure" role="alert"><p>{run.error}</p>{index === conversation.runs.length - 1 ? <button type="button" className="qa-quiet-button" onClick={() => void retry(run)} disabled={busy}>重新回答</button> : null}</div> : null}
              {run.result?.budget_exhausted || run.result?.evidence_gaps?.length || run.result?.conditions.length ? <aside className="qa-conditions" aria-label="适用限制"><TriangleAlert size={17} aria-hidden="true" /><div>
                {run.result.evidence_gaps?.length ? <ul>{run.result.evidence_gaps.map((gap, i) => <li key={i}>{gap}</li>)}</ul> : run.result.budget_exhausted ? <p>本次分析已达处理上限</p> : null}
                {run.result.conditions.length ? <ul>{run.result.conditions.map((condition, i) => <li key={i}>{condition}</li>)}</ul> : null}
              </div></aside> : null}
              </QAAnswer>
              {run.result?.questions?.length ? index === conversation.runs.length - 1
                ? <QAQuestions questions={run.result.questions} disabled={busy} onSubmit={send} />
                : <div className="qa-question-history">{run.result.questions.map((item, i) => <p key={i}>{item.question}</p>)}</div>
                : null}
              {run.status === 'completed' && !run.result?.questions?.length ? <footer className="qa-answer-footer"><button type="button" className="qa-handoff-button" onClick={() => { setHandoffRun(run); setHandoffMaterials(run.material_ids); }}>发起合规审查<ArrowRight size={15} /></button></footer> : null}
              {handoffRun?.id === run.id ? <div className="qa-handoff"><h3>带入正式审查</h3><p>正式审查将重新核实事实与法规依据。</p>{conversation.materials.filter((m) => run.material_ids.includes(m.id)).map((m) => <label key={m.id}><input type="checkbox" checked={handoffMaterials.includes(m.id)} onChange={(event) => setHandoffMaterials((ids) => event.target.checked ? [...ids, m.id] : ids.filter((id) => id !== m.id))} />{m.filename}</label>)}<div><button type="button" className="btn-primary" disabled={preparing} onClick={() => void prepareHandoff()}>{preparing ? '正在准备材料…' : '前往工作台'}</button><button type="button" className="qa-quiet-button" onClick={() => setHandoffRun(null)}>取消</button></div></div> : null}
            </div>
          </article>
        ))}
        <div ref={end} />
      </div>

      <div className="qa-composer-wrap">
        {error ? <div className="error-box" role="alert">{error}</div> : null}
        {conversation?.materials.length ? <details className="qa-material-picker"><summary>本轮使用的材料 · {selected.length}</summary>{conversation.materials.map((m) => <label key={m.id}><input type="checkbox" disabled={busy} checked={selected.includes(m.id)} onChange={(event) => setSelected((ids) => event.target.checked ? [...ids, m.id] : ids.filter((id) => id !== m.id))} />{m.filename}</label>)}</details> : null}
        <form className={'qa-composer' + (draggingFiles ? ' is-dragging' : '')} onSubmit={(event) => { event.preventDefault(); void send(); }} onDragEnter={() => { dragDepthRef.current += 1; setDraggingFiles(true); }} onDragLeave={() => { dragDepthRef.current = Math.max(0, dragDepthRef.current - 1); if (dragDepthRef.current === 0) setDraggingFiles(false); }} onDragOver={(event) => event.preventDefault()} onDrop={(event) => { event.preventDefault(); dragDepthRef.current = 0; setDraggingFiles(false); try { const incoming = Array.from(event.dataTransfer?.files ?? []); if (incoming.length) { incoming.forEach(validateUploadFile); setFiles((items) => [...items, ...incoming]); setError(null); } } catch (reason) { setError(reason instanceof Error ? reason.message : '文件无效'); } }}>
          {draggingFiles ? <div className="qa-drag-overlay" aria-hidden="true"><Paperclip size={22} strokeWidth={1.8} /><span>松开以添加材料</span></div> : null}
          <label htmlFor="qa-question" className="sr-only">合规问题</label>
          <textarea ref={questionInput} id="qa-question" value={question} onChange={(event) => setQuestion(event.target.value)} maxLength={20000} rows={hasMessages ? 1 : 3} placeholder={hasMessages ? '继续提问，或补充业务情况…' : '请描述业务场景或合规问题…'} disabled={loading || sending} onKeyDown={(event) => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) { event.preventDefault(); void send(); } }} />
          {files.length ? <div className="qa-file-chips">{files.map((file, i) => <span key={i}><FileText size={14} />{file.name}<button type="button" disabled={sending} aria-label={`移除 ${file.name}`} onClick={() => setFiles((items) => items.filter((_, index) => index !== i))}><X size={13} /></button></span>)}</div> : null}
          <div className="qa-composer-toolbar"><button type="button" className="qa-attach" title="添加材料" aria-label="添加材料" disabled={busy} onClick={() => input.current?.click()}><Paperclip size={16} /></button><input ref={input} type="file" hidden multiple accept=".txt,.md,.pdf,.docx,.xlsx,.csv,.html,.htm" onChange={(event) => { try { const incoming = Array.from(event.target.files ?? []); incoming.forEach(validateUploadFile); setFiles((items) => [...items, ...incoming]); setError(null); } catch (reason) { setError(reason instanceof Error ? reason.message : '文件无效'); } event.target.value = ''; }} /><button className="qa-send" type="submit" aria-label="发送问题" title="发送问题（Ctrl / ⌘ + Enter）" disabled={!question.trim() || busy}><ArrowUp size={20} /></button></div>
        </form>
        {!hasMessages && !loading ? <div className="qa-examples" aria-label="示例问题">{EXAMPLES.map((example) => <button key={example} type="button" disabled={busy} onClick={() => { setQuestion(example); questionInput.current?.focus(); }}>{example}</button>)}</div> : null}
        {!hasMessages && !loading ? <div className="qa-guide-entry"><button type="button" className="qa-quiet-button" disabled={busy} aria-expanded={showGuide} onClick={() => setShowGuide((value) => !value)}><CircleHelp size={15} />使用指引</button></div> : null}
        {!hasMessages && !loading && showGuide ? <QaOnboarding userRole={userRole} onDismiss={() => setShowGuide(false)} /> : null}
      </div>
    </section>
  );
}
