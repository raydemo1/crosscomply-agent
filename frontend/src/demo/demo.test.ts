import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { createServer, type ViteDevServer } from 'vite';
import type { MatterStreamEvent } from '../utils/matterStream.ts';

let server: ViteDevServer;
let client: typeof import('../api/client.ts');
let examples: typeof import('./examples.ts');
let networkCalls = 0;
const originalFetch = globalThis.fetch;
before(async () => {
  server = await createServer({ mode: 'demo', server: { middlewareMode: true, hmr: false }, appType: 'custom' });
  client = await server.ssrLoadModule('/src/api/client.ts');
  examples = await server.ssrLoadModule('/src/demo/examples.ts');
  globalThis.fetch = async () => { networkCalls += 1; throw new Error('A demo must never fetch an API'); };
});
after(async () => { globalThis.fetch = originalFetch; await server?.close(); });

test('public cases are synthetic, consistent and readable without an API', async () => {
  for (const saved of examples.exampleCases) {
    const detail = await client.getCaseDetail(saved.id);
    assert.match(detail.case.material_text, /虚构示例材料/);
    assert.match(detail.case.id, /^demo-/);
    assert.equal(detail.review_task?.material_snapshot_id, detail.material_snapshot?.id);
    assert.equal(detail.review_task?.intake_snapshot_id, detail.intake_snapshot?.id);
    assert.equal(detail.review_task?.attempt_count, 0);
    assert.equal(detail.signed_decision, null);
    assert.equal(detail.report, null);
    assert.equal((await client.getReviewMaterials(saved.id))[0].parsed_text, detail.case.material_text);
  }
  assert.equal(networkCalls, 0);
});

test('unknown endpoints and mutations stop locally without reaching a service', async () => {
  await assert.rejects(client.getCaseDetail('real-case'), /仅提供/);
  await assert.rejects(client.listCases(), /仅提供/);
  await assert.rejects(client.saveFeedback(examples.exampleCases[0].id, { notes: 'real notes' }), /真实提交与审查未开放/);
  assert.equal(networkCalls, 0);
});

test('example streaming delivers deltas before completion and isolates case history', async () => {
  const saved = examples.exampleCases[0];
  const task = saved.reviewTask!;
  const events: MatterStreamEvent[] = [];
  await client.streamMatterAnswer(saved.id, { task_id: task.id, material_snapshot_id: task.material_snapshot_id, intake_snapshot_id: task.intake_snapshot_id, question: '解释这段内容' }, (event) => events.push(event));
  assert.ok(events.filter((event) => event.type === 'delta').length > 5);
  const done = events.filter((event) => event.type === 'done');
  assert.equal(done.length, 1);
  assert.ok(events.findIndex((event) => event.type === 'delta') < events.findIndex((event) => event.type === 'done'));
  assert.match(done[0].turn.reply.answer, /预先编写的示例回答/);
  assert.deepEqual(done[0].turn.reply.proposed_facts, {});
  assert.equal((await client.getMatterConversation(saved.id)).length, 1);
  assert.equal((await client.getMatterConversation(examples.exampleCases[1].id)).length, 0);
  assert.equal(networkCalls, 0);
});

test('cancelled streams are not saved and mismatched versions are rejected', async () => {
  const saved = examples.exampleCases[1];
  const task = saved.reviewTask!;
  const binding = { task_id: task.id, material_snapshot_id: task.material_snapshot_id, intake_snapshot_id: task.intake_snapshot_id, question: '解释这段内容' };
  const abort = new AbortController();
  await assert.rejects(client.streamMatterAnswer(saved.id, binding, (event) => { if (event.type === 'delta') abort.abort(); }, abort.signal), { name: 'AbortError' });
  assert.equal((await client.getMatterConversation(saved.id)).length, 0);
  await assert.rejects(client.streamMatterAnswer(saved.id, { ...binding, task_id: 'other' }, () => {}), /版本不匹配/);
  assert.equal(networkCalls, 0);
});


test('public deployment prohibits connections and pins its theme script hash', () => {
  const config = JSON.parse(readFileSync(new URL('../../vercel.json', import.meta.url), 'utf8'));
  const policy = config.headers[0].headers.find((header: { key: string }) => header.key === 'Content-Security-Policy').value;
  assert.match(policy, /connect-src 'none'/);
  assert.equal(config.buildCommand, 'npm run build:demo');
  const html = readFileSync(new URL('../../index.html', import.meta.url), 'utf8');
  const script = /<script>([\s\S]*?)<\/script>/.exec(html)![1].replace(/\r\n/g, '\n');
  const hash = createHash('sha256').update(script).digest('base64');
  assert.ok(policy.includes(`'sha256-${hash}'`));
  assert.doesNotMatch(policy, /script-src[^;]*'unsafe-inline'/);
});
