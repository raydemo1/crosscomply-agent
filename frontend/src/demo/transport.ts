import type { MatterSelection, MatterTurnApi } from '../types/api';
import type { FactBinding } from '../api/client';
import type { MatterStreamEvent } from '../utils/matterStream';
import { scenarios, exampleDetail } from './examples';

const conversations = new Map<string, MatterTurnApi[]>();

export async function demoRequest(path: string, options: RequestInit = {}): Promise<unknown> {
  if (options.method && options.method !== 'GET') throw new Error('当前为示例演示，真实提交与审查未开放。');
  const match = /^\/api\/cases\/([^/?]+)(?:\/([^?]+))?(?:\?.*)?$/.exec(path);
  const scenario = scenarios.find((item) => item.id === match?.[1]);
  if (!scenario) throw new Error('演示站仅提供已编写的示例案例。');
  const detail = exampleDetail(scenario);
  switch (match?.[2]) {
    case undefined: return structuredClone(detail);
    case 'conversation': return { items: structuredClone(conversations.get(scenario.id) ?? []) };
    case 'knowledge-rechecks':
    case 'annotations': return { items: [] };
    case 'review-materials': return { items: [{ id: `${scenario.id}-material-1`, logical_name: '虚构业务说明', filename: `${scenario.title}（虚构示例）.txt`, version_number: 1, parsed_text: scenario.material }] };
    default: throw new Error('此操作需要正式服务，示例演示未开放。');
  }
}

export async function streamExampleAnswer(
  caseId: string,
  payload: FactBinding & { question: string; selection?: MatterSelection },
  onEvent: (event: MatterStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const scenario = scenarios.find((item) => item.id === caseId);
  if (!scenario) throw new Error('未找到示例案例。');
  if (payload.task_id !== `${caseId}-task` || payload.material_snapshot_id !== `${caseId}-materials` || payload.intake_snapshot_id !== `${caseId}-intake`) throw new Error('示例版本不匹配，请重新打开案例。');
  if (!payload.question.trim() || payload.question.length > 3000) throw new Error('请输入 3000 字以内的问题。');
  signal?.throwIfAborted();
  onEvent({ type: 'reset' });
  onEvent({ type: 'status', message: '正在展示预编写的示例回答…' });
  for (const text of scenario.answer.match(/.{1,14}|\n/gu) ?? []) {
    signal?.throwIfAborted();
    await new Promise<void>((resolve, reject) => {
      const cancel = () => { clearTimeout(timer); reject(signal?.reason ?? new DOMException('已取消', 'AbortError')); };
      const timer = setTimeout(() => { signal?.removeEventListener('abort', cancel); resolve(); }, 24);
      signal?.addEventListener('abort', cancel, { once: true });
      if (signal?.aborted) { signal.removeEventListener('abort', cancel); cancel(); }
    });
    signal?.throwIfAborted();
    onEvent({ type: 'delta', text });
  }
  const turn: MatterTurnApi = {
    id: `demo-turn-${crypto.randomUUID()}`, task_id: payload.task_id, material_snapshot_id: payload.material_snapshot_id, intake_snapshot_id: payload.intake_snapshot_id,
    question: payload.question, selection: payload.selection, created_at: new Date().toISOString(),
    reply: { answer: scenario.answer, claims: [], material_citations: [], fact_fields: [], proposed_facts: {} },
  };
  conversations.set(caseId, [...(conversations.get(caseId) ?? []).slice(-19), turn]);
  onEvent({ type: 'done', turn });
}
