import type { MatterTurnApi } from '../types/api.ts';

export type MatterStreamEvent =
  | { type: 'status'; message: string }
  | { type: 'reset' }
  | { type: 'delta'; text: string }
  | { type: 'done'; turn: MatterTurnApi }
  | { type: 'error'; message: string };

export async function readMatterStream(body: ReadableStream<Uint8Array>, onEvent: (event: MatterStreamEvent) => void): Promise<void> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let terminal = false;
  const emit = (block: string) => {
    const data = block.split(/\r?\n/).filter((line) => line.startsWith('data:')).map((line) => line.slice(5).trimStart()).join('\n');
    if (!data) return;
    const event = JSON.parse(data) as MatterStreamEvent;
    if (event.type === 'done' && (!event.turn?.id || typeof event.turn.reply?.answer !== 'string')) throw new Error('回答未能完整保存，请重试');
    if (event.type === 'delta' && typeof event.text !== 'string') throw new Error('回答数据异常，请重试');
    if (!['status', 'reset', 'delta', 'done', 'error'].includes(event.type)) throw new Error('回答数据异常，请重试');
    terminal = event.type === 'done' || event.type === 'error';
    onEvent(event);
  };
  try {
    while (!terminal) {
      const { done, value } = await reader.read();
      buffer += decoder.decode(value, { stream: !done });
      let separator: RegExpExecArray | null;
      while (!terminal && (separator = /\r?\n\r?\n/.exec(buffer))) {
        emit(buffer.slice(0, separator.index));
        buffer = buffer.slice(separator.index + separator[0].length);
      }
      if (done) {
        if (!terminal && buffer.trim()) emit(buffer);
        break;
      }
    }
    if (!terminal) throw new Error('连接已中断，回答尚未完成，请重试');
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
