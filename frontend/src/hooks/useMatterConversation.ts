import { useEffect, useRef, useState } from 'react';
import { askMatterAgent, confirmCaseFacts, getMatterConversation, waitForReviewTask } from '../api/client';
import type { MatterTurnApi } from '../types/api';
import type { SavedCase } from '../types/case';
import { openCase } from '../store/caseStore';

export function useMatterConversation(saved: SavedCase) {
  const task = saved.reviewTask;
  const key = `${saved.id}/${task?.id}/${task?.material_snapshot_id}/${task?.intake_snapshot_id}`;
  const current = useRef(key);
  current.current = key;
  const mounted = useRef(true);
  const [turns, setTurns] = useState<MatterTurnApi[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);
  useEffect(() => {
    let cancelled = false;
    setTurns([]); setError(''); setBusy(false);
    if (!task) { setLoading(false); return; }
    setLoading(true);
    getMatterConversation(saved.id).then((items) => {
      if (cancelled || current.current !== key) return;
      setTurns((existing) => [...new Map([...items.filter((t) => t.task_id === task.id && t.material_snapshot_id === task.material_snapshot_id && t.intake_snapshot_id === task.intake_snapshot_id), ...existing].map((t) => [t.id, t])).values()]);
    }).catch((e: unknown) => {
      if (!cancelled) setError(e instanceof Error ? e.message : '暂时无法读取对话');
    }).finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [key]);
  const binding = task ? { task_id: task.id, material_snapshot_id: task.material_snapshot_id, intake_snapshot_id: task.intake_snapshot_id } : null;
  const run = async (action: () => Promise<void>) => {
    if (busy || !binding) return false;
    setBusy(true); setError('');
    try { await action(); return mounted.current && current.current === key; } catch (e: unknown) {
      if (mounted.current && current.current === key) setError(e instanceof Error ? e.message : '操作未完成，请重试');
      return false;
    } finally { if (mounted.current && current.current === key) setBusy(false); }
  };
  const ask = (question: string) => run(async () => {
    const turn = await askMatterAgent(saved.id, { ...binding!, question });
    if (mounted.current && current.current === key) {
      setTurns((existing) => [...existing, turn]);
      await openCase(saved.id);
    }
  });
  const confirm = (turn: MatterTurnApi) => run(async () => {
    const queued = await confirmCaseFacts(saved.id, { ...binding!, conversation_id: turn.id, values: turn.reply.proposed_facts, confirmed: true });
    if (!mounted.current || current.current !== key) return;
    await openCase(saved.id);
    await waitForReviewTask(queued.task_id, async () => { if (mounted.current) await openCase(saved.id); });
    if (mounted.current) await openCase(saved.id);
  });
  return { turns, busy, loading, error, ask, confirm, key };
}
