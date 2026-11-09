import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createElement, Fragment } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer, type ViteDevServer } from 'vite';
import type { ReviewResult } from '../types/api.ts';
import type { SavedCase } from '../types/case.ts';
import { partitionReportContent } from '../utils/reportContent.ts';

let server: ViteDevServer;
let reportComponents: typeof import('./ReportConclusion.tsx');
let buildHtmlReport: (saved: SavedCase) => string;
let buildMarkdownReport: (saved: SavedCase) => string;
before(async () => {
  server = await createServer({ server: { middlewareMode: true }, appType: 'custom' });
  reportComponents = await server.ssrLoadModule('/src/components/ReportConclusion.tsx');
  ({ buildHtmlReport, buildMarkdownReport } = await server.ssrLoadModule('/src/utils/report.ts'));
});
after(async () => { await server?.close(); });

const result = {
  risk_level: 'insufficient_evidence', legal_path: null, decision_summary: '人数尚未确认，暂不能确定出境路径。',
  conclusion: '### 个案判断\n\n**人数尚未确认，暂不能确定出境路径。**<sup data-citation-ref="法源-01">①</sup>\n\n| 条件 | 本案情况 | 判断 |\n| --- | --- | --- |\n| 人数 | 未提供 | 待核实 |',
  trigger_reasons: ['**人数未确认**，不能选择路径。'], risk_boundaries: ['仅适用于当前材料。', '本报告不构成正式法律意见。'],
  claims: [], recommended_actions: [], missing_information: [], legal_basis_gaps: [],
} as unknown as ReviewResult;

function renderReport(result: ReviewResult) {
  const { body, conditions } = partitionReportContent(result.conclusion, result.risk_boundaries);
  return renderToStaticMarkup(createElement(Fragment, null,
    createElement(reportComponents.default, { result, conditions }),
    createElement(reportComponents.ReportAnalysis, { body, onCitationClick: () => {} }),
  ));
}

test('report hierarchy preserves conditions, highlights and accessible citations/tables', () => {
  const html = renderReport(result);
  assert.ok(html.indexOf('审查摘要') < html.indexOf('结论适用前提与范围'));
  assert.ok(html.indexOf('结论适用前提与范围') < html.indexOf('判断要点'));
  assert.ok(html.indexOf('判断要点') < html.indexOf('完整分析'));
  assert.match(html, /<strong>人数尚未确认，暂不能确定出境路径。<\/strong>/);
  assert.match(html, /class="cite-marker"/);
  assert.match(html, /role="region" aria-label="报告表格，可横向滚动" tabindex="0"/);
  assert.doesNotMatch(html, /当前适用路径|使用说明|本报告不构成正式法律意见/);
});

test('empty optional sections stay absent and legacy prose remains readable', () => {
  const html = renderReport({ ...result, conclusion: '历史报告原文。', trigger_reasons: [], risk_boundaries: [] });
  assert.match(html, /历史报告原文。/);
  assert.doesNotMatch(html, /判断要点|结论适用前提与范围|使用说明/);
});

test('all exporters keep the same full summary, body and substantive boundaries', () => {
  const saved = { id: 'fixture', status: 'approved', savedAt: '2026-10-26T10:00:00+08:00', question: '测试目标', materialText: '固定材料', intake: { important_data_status: 'unknown', ciio_status: 'unknown' }, feedback: null, response: {
    trace_id: 'trace-fixture', review_case_id: 'review-fixture', review_result: result,
    review_facts: { data_types: [], regions: [], missing_information: [] }, citation_groups: [],
    evidence_self_check: { status: 'insufficient', triggered_reasons: [], issues: [], second_retrieval_triggered: false },
  } } as unknown as SavedCase;
  const html = buildHtmlReport(saved);
  const markdown = buildMarkdownReport(saved);
  assert.match(html, /<table>[\s\S]*<td>待核实<\/td>/);
  assert.match(html, /<strong>人数尚未确认，暂不能确定出境路径。<\/strong>/);
  assert.ok(markdown.includes(result.conclusion));
  for (const output of [html, markdown]) {
    assert.ok(output.includes(result.decision_summary));
    assert.ok(output.includes('仅适用于当前材料。'));
    assert.ok(output.includes('使用说明'));
  }
});
