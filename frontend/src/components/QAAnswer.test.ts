import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';
import type { ViteDevServer } from 'vite';
import type { ComponentType } from 'react';

let server: ViteDevServer;
let QAAnswer: ComponentType<{ answer: string; result: null; streaming: boolean; incomplete?: boolean }>;
before(async () => {
  server = await createServer({ server: { middlewareMode: true }, appType: 'custom' });
  QAAnswer = (await server.ssrLoadModule('/src/components/QAAnswer.tsx')).default;
});
after(async () => { await server?.close(); });

test('interrupted legal prose is collapsed plaintext with no live citation or Markdown', () => {
  const html = renderToStaticMarkup(createElement(QAAnswer, {
    answer: '**无需审批** <a href="https://example.test">依据</a>', result: null, streaming: false, incomplete: true,
  }));
  assert.match(html, /<details class="qa-incomplete-draft">/);
  assert.doesNotMatch(html, /<details[^>]*\bopen|qa-prose|<strong>|<a /);
  assert.match(html, /&lt;a href=/);
  assert.match(html, /可能缺少条件和例外/);
});

test('normal streaming prose retains Markdown rendering', () => {
  const html = renderToStaticMarkup(createElement(QAAnswer, {
    answer: '**正在解释**', result: null, streaming: true,
  }));
  assert.match(html, /qa-prose is-streaming/);
  assert.match(html, /<strong>正在解释<\/strong>/);
  assert.doesNotMatch(html, /qa-incomplete-draft/);
});
