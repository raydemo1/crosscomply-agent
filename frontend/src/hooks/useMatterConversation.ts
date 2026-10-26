import { useEffect, useRef, useState } from 'react';
import { confirmCaseFacts, getMatterConversation, streamMatterAnswer, waitForReviewTask } from '../api/client';
import type { MatterSelection, MatterTurnApi } from '../types/api';
import type { SavedCase } from '../types/case';
import { openCase } from '../store/caseStore';

export interface MatterStreamingTurn {
  question: string;
  selection?: MatterSelection;
  text: string;
  error: string;
  status: string;
}

export function useMatterConversation(saved: SavedCase) {
  const task = saved.reviewTask;
  const key = `${saved.id}/${task?.id}/${task?.material_snapshot_id}/${task?.intake_snapshot_id}`;
  const current = useRef(key);
  current.current = key;
  const mounted = useRef(true);
  const running = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const [turns, setTurns] = useState<MatterTurnApi[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [streaming, setStreaming] = useState<MatterStreamingTurn | null>(null);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; controller.current?.abort(); };
  }, []);
  useEffect(() => {
    let cancelled = false;
    controller.current?.abort();
    running.current = false;
    setTurns([]); setError(''); setBusy(false); setStreaming(null);
    if (!task) { setLoading(false); return; }
    setLoading(true);
    getMatterConversation(saved.id).then((items) => {
      if (cancelled || current.current !== key) return;
      setTurns((existing) => [...new Map([...items.filter((t) => t.task_id === task.id && t.material_snapshot_id === task.material_snapshot_id && t.intake_snapshot_id === task.intake_snapshot_id), ...existing].map((t) => [t.id, t])).values()]);
    }).catch((e: unknown) => {
      if (!cancelled) setError(e instanceof Error ? e.message : '暂时无法读取对话');
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; controller.current?.abort(); };
  }, [key]);
  const binding = task ? { task_id: task.id, material_snapshot_id: task.material_snapshot_id, intake_snapshot_id: task.intake_snapshot_id } : null;
  const isCurrent = () => mounted.current && current.current === key;
  const run = async (action: () => Promise<void>) => {
    if (running.current || !binding) return false;
    running.current = true;
    setBusy(true); setError('');
    try { await action(); return isCurrent(); } catch (e: unknown) {
      if (isCurrent()) setError(e instanceof Error ? e.message : '操作未完成，请重试');
      return false;
    } finally {
      if (isCurrent()) { running.current = false; setBusy(false); }
    }
  };
  const ask = (question: string, selection?: MatterSelection) => run(async () => {
    const abort = new AbortController();
    controller.current = abort;
    setStreaming({ question, selection, text: '', error: '', status: '正在准备回答…' });
    try {
      await streamMatterAnswer(saved.id, { ...binding!, question, selection }, (evt) => {
        if (!isCurrent()) return;
        if (evt.type === 'delta') setStreaming((s) => s ? { ...s, text: s.text + evt.text } : s);
        else if (evt.type === 'reset') setStreaming((s) => s ? { ...s, text: '', status: '正在生成回答…' } : s);
        else if (evt.type === 'status') setStreaming((s) => s ? { ...s, status: evt.message } : s);
        else if (evt.type === 'error') setStreaming((s) => s ? { ...s, error: evt.message } : s);
        else if (evt.type === 'done') {
          setTurns((items) => [...new Map([...items, evt.turn].map((turn) => [turn.id, turn])).values()]);
          setStreaming(null);
        }
      }, abort.signal);
    } catch (e: unknown) {
      if (!isCurrent() || abort.signal.aborted) return;
      const message = e instanceof Error && e.name === 'TimeoutError' ? '回答超时，请重试' : e instanceof Error ? e.message : '连接已中断，请重试';
      setStreaming((s) => s ? { ...s, error: message } : s);
    }
  });
  const retryStreaming = () => {
    if (streaming && !running.current) void ask(streaming.question, streaming.selection);
  };
  const confirm = (turn: MatterTurnApi) => run(async () => {
    const queued = await confirmCaseFacts(saved.id, { ...binding!, conversation_id: turn.id, values: turn.reply.proposed_facts, confirmed: true });
    if (!isCurrent()) return;
    await openCase(saved.id);
    await waitForReviewTask(queued.task_id, async () => { if (mounted.current) await openCase(saved.id); });
    if (mounted.current) await openCase(saved.id);
  });
  return { turns, busy, loading, error, streaming, ask, retryStreaming, confirm, key };
}