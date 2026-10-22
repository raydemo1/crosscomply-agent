import assert from 'node:assert/strict';
import test from 'node:test';
import { citationLocation, citationURL, citedQACitationGroups, groupQACitations, remarkQACitations } from './qaCitations.ts';
import type { QACitation } from '../types/qa';

const citation = (ref: string, updates: Partial<QACitation> = {}): QACitation => ({
  ref, title: '测试法规', source_id: 'law1', url: 'https://example.test/law',
  kind: 'legal', excerpt: '原文', ...updates,
});

test('same document groups passages while preserving distinct versions and citation identities', () => {
  const citations = [citation('依据1', { article_no: '第一条' }), citation('依据2', { article_no: '第二条' }),
    citation('依据3', { effective_date: '2023-01-01' })];
  const groups = groupQACitations(citations);
  assert.equal(groups.length, 2);
  assert.deepEqual(groups[0].citations, citations.slice(0, 2));
  assert.deepEqual(groups.map((group) => group.number), [1, 2]);
  assert.equal(citationLocation(groups[0].citations[1]), '第二条');
});

test('a shared URL does not merge different governed sources', () => {
  assert.equal(groupQACitations([citation('依据1'), citation('依据2', { source_id: 'law2' })]).length, 2);
});

test('source location uses supplied article or heading and does not invent line numbers', () => {
  assert.equal(citationLocation(citation('依据1', { heading_path: ['说明', '适用范围'] })), '说明 / 适用范围');
  assert.equal(citationLocation(citation('依据1')), '原文片段');
  assert.equal(citationURL('javascript:alert(1)'), undefined);
  assert.equal(citationURL('https://example.test/law'), 'https://example.test/law');
});

test('adjacent references become one link without relocating paragraph content', () => {
  const tree = { type: 'root', children: [{ type: 'paragraph', children: [{ type: 'text', value: '条件成立时适用。[依据1][依据2][依据1] 后文。' }] }] };
  remarkQACitations({ refs: ['依据1', '依据2'] })(tree);
  assert.deepEqual(tree.children[0].children, [
    { type: 'text', value: '条件成立时适用。' },
    { type: 'link', url: '#qa-cite=1,2', children: [{ type: 'text', value: '依据1、依据2' }] },
    { type: 'text', value: ' 后文。' },
  ]);
});

test('incomplete and unknown references stay visible instead of gaining a false source link', () => {
  for (const text of ['[依据', '[依据1][依据999]', '[依据9]']) {
    const tree = { type: 'root', children: [{ type: 'text', value: text }] };
    remarkQACitations({ refs: ['依据1'] })(tree);
    assert.deepEqual(tree.children, [{ type: 'text', value: text }]);
  }
});

test('literal citations inside code and existing links are not rewritten', () => {
  const tree = { type: 'root', children: [
    { type: 'code', value: '[依据1]' },
    { type: 'inlineCode', value: '[依据1]' },
    { type: 'link', url: 'https://example.test', children: [{ type: 'text', value: '[依据1]' }] },
  ] };
  const snapshot = structuredClone(tree);
  remarkQACitations({ refs: ['依据1'] })(tree);
  assert.deepEqual(tree, snapshot);
});

test('only cited passages and versions appear in sources while document numbers stay stable', () => {
  const groups = groupQACitations([
    citation('依据1', { source_id: 'unused' }), citation('依据2'), citation('依据3'),
    citation('依据4', { effective_date: '2023-01-01' }),
  ]);
  const visible = citedQACitationGroups('本段结论。[依据3]', groups);
  assert.equal(visible.length, 1);
  assert.equal(visible[0].number, 2);
  assert.deepEqual(visible[0].citations.map((c) => c.ref), ['依据3']);
  assert.deepEqual(groups[1].citations.map((c) => c.ref), ['依据2', '依据3']);
});

test('streaming citations appear only after a complete valid marker', () => {
  const groups = groupQACitations([citation('依据1')]);
  assert.deepEqual(citedQACitationGroups('回答[依据1', groups), []);
  assert.equal(citedQACitationGroups('回答[依据1]', groups).length, 1);
  assert.deepEqual(citedQACitationGroups('[依据1][依据999]', groups), []);
});

test('code, links, HTML and incomplete markers do not add reference sources', () => {
  const groups = groupQACitations([citation('依据1')]);
  for (const answer of ['`[依据1]`', '```text\n[依据1]\n```', '[[依据1]](https://example.test)',
    '<!-- [依据1] -->', '[依据1', '[依据9]']) {
    assert.deepEqual(citedQACitationGroups(answer, groups), [], answer);
  }
});

test('citations in formatted paragraphs and GFM tables match the visible source list', () => {
  const groups = groupQACitations([citation('依据1'), citation('依据2', { source_id: 'law2' })]);
  const answer = '**结论**[依据1]\n\n| 判断 | 依据 |\n| --- | --- |\n| 条件 | [依据2] |';
  assert.deepEqual(citedQACitationGroups(answer, groups).map((g) => g.number), [1, 2]);
});
