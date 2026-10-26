import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readMatterStream, type MatterStreamEvent } from './matterStream.ts';

const turn = { id: 'turn', reply: { answer: '中文\n回答' } };
const data = (event: unknown) => `data: ${JSON.stringify(event)}\r\n\r\n`;
function stream(bytes: Uint8Array, split: number) {
  return new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(bytes.slice(0, split)); controller.enqueue(bytes.slice(split)); controller.close(); } });
}
test('decodes every byte split, CRLF, Chinese, reset and terminal turn once', async () => {
  const source = new TextEncoder().encode(': keepalive\r\n\r\n' + data({ type: 'status', message: '准备' }) + data({ type: 'delta', text: '中' }) + data({ type: 'reset' }) + data({ type: 'delta', text: '中文\n回答' }) + data({ type: 'done', turn }) + data({ type: 'done', turn }));
  for (let split = 0; split <= source.length; split++) {
    const events: MatterStreamEvent[] = [];
    await readMatterStream(stream(source, split), (event) => events.push(event));
    assert.deepEqual(events.map((e) => e.type), ['status', 'delta', 'reset', 'delta', 'done']);
    assert.equal((events[3] as { text: string }).text, '中文\n回答');
  }
});
test('delivers partial content and rejects EOF without done', async () => {
  const events: MatterStreamEvent[] = [];
  await assert.rejects(readMatterStream(stream(new TextEncoder().encode(data({ type: 'delta', text: '保留正文' })), 4), (event) => events.push(event)), /连接已中断/);
  assert.equal((events[0] as { text: string }).text, '保留正文');
});
test('error ends stream; malformed JSON and malformed done are rejected', async () => {
  for (const source of ['data: invalid\n\n', data({ type: 'done', turn: {} })]) {
    await assert.rejects(readMatterStream(stream(new TextEncoder().encode(source), 2), () => {}));
  }
  const events: MatterStreamEvent[] = [];
  await readMatterStream(stream(new TextEncoder().encode(data({ type: 'error', message: '重试' })), 2), (event) => events.push(event));
  assert.deepEqual(events, [{ type: 'error', message: '重试' }]);
});
