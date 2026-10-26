import assert from 'node:assert/strict';
import { test } from 'node:test';
import { partitionReportContent } from './reportContent.ts';
import { renderReportMarkdown } from './reportHtml.ts';

test('only whole known disclaimers move; substantive qualifications stay complete', () => {
  const note = '本结论基于当前材料及已召回证据，不构成正式法律意见。';
  const boundary = '人数尚未确认，本报告不构成正式法律意见，且未评估境外法律。';
  const result = partitionReportContent(`### 判断\n\n**应先确认人数。**\n\n**${note}**`, [note, boundary, boundary]);
  assert.equal(result.body, '### 判断\n\n**应先确认人数。**');
  assert.deepEqual(result.conditions, [boundary]);
  assert.deepEqual(result.usageNotes, [note]);
});

test('historical text and unusual or long boundaries are not rewritten or clipped', () => {
  const conclusion = '原有纯文本。\n\n未核实范围，不能确定结论。';
  const boundary = '适用条件'.repeat(200);
  assert.deepEqual(partitionReportContent(conclusion, [boundary]), { body: conclusion, conditions: [boundary], usageNotes: [] });
});

test('HTML export renders GFM and preserves program citations without raw HTML or unsafe links', () => {
  const result = renderReportMarkdown('### 规则\n\n**条件成立时才适用**<sup class="cite-marker" id="cite-marker-0-1" data-claim-index="0" data-citation-ref="法源-01">①</sup>\n\n- 要点\n\n| 条件 | 判断 |\n| --- | --- |\n| 未确认 | 待核实 |\n\n<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[危险](javascript:alert%281%29)');
  assert.match(result, /<h3>规则<\/h3>/);
  assert.match(result, /<strong>条件成立时才适用<\/strong>/);
  assert.match(result, /<table>/);
  assert.match(result, /<li>要点<\/li>/);
  assert.match(result, /href="#%E6%B3%95%E6%BA%90-01"/);
  assert.doesNotMatch(result, /<script|<img|onerror=|href="javascript:/);
});
