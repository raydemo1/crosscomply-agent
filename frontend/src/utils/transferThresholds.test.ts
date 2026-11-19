import assert from 'node:assert/strict';
import test from 'node:test';
import { formatHeadcount, parseHeadcount, transferThresholds } from './transferThresholds.ts';

test('headcounts parse plain numbers, separators and 万', () => {
  assert.equal(parseHeadcount('350000'), 350_000);
  assert.equal(parseHeadcount('350,000'), 350_000);
  assert.equal(parseHeadcount('约35万人'), 350_000);
  assert.equal(parseHeadcount(8000), 8000);
  assert.equal(parseHeadcount('unknown'), null);
  assert.equal(parseHeadcount(''), null);
  assert.equal(parseHeadcount(null), null);
  assert.equal(formatHeadcount(1_000_000), '100万');
  assert.equal(formatHeadcount(8000), '8,000');
});

test('the case lands in the standard-contract zone with the gap to assessment', () => {
  const view = transferThresholds({ annual_non_sensitive_count: '350000', annual_sensitive_count: '8000', count_period: 'current_year_cumulative', ciio_status: 'not_ciio' });
  assert.equal(view.blockedReason, null);
  assert.deepEqual(view.caveats, ['是否涉及重要数据尚未确认；如涉及，须申报安全评估。']);
  const [general, sensitive] = view.scales;
  assert.equal(general.zoneLabel, '标准合同 / 认证');
  assert.equal(general.nextGap, 650_000);
  assert.equal(general.max, 1_200_000);
  assert.equal(sensitive.zoneLabel, '标准合同 / 认证');
  assert.equal(sensitive.nextGap, 2000);
  assert.equal(sensitive.nextLabel, '安全评估');
  // 等宽区间：35 万在第二档（1/3–2/3）内 250k/900k 处；8,000 在第一档 80% 处。
  assert.ok(Math.abs(general.marker - (1 / 3 + (250_000 / 900_000) / 3)) < 1e-9);
  assert.ok(Math.abs(sensitive.marker - 0.4) < 1e-9);
  assert.deepEqual(general.ticks.map((tick) => tick.at), [1 / 3, 2 / 3]);
});

test('values beyond the default range extend the scale and sit in the top zone', () => {
  const [general] = transferThresholds({ annual_non_sensitive_count: '3000000' }).scales;
  assert.ok(general.max >= 3_000_000);
  assert.equal(general.zoneLabel, '安全评估');
  assert.equal(general.nextGap, null);
});

test('CIIO and important data bypass the headcount scale', () => {
  assert.match(transferThresholds({ ciio_status: 'ciio', annual_non_sensitive_count: '5000' }).blockedReason ?? '', /安全评估/);
  assert.equal(transferThresholds({ important_data_status: 'important' }).scales.length, 0);
});

test('missing counts give no scale; unconfirmed premises carry caveats', () => {
  assert.equal(transferThresholds({}).scales.length, 0);
  assert.deepEqual(transferThresholds({}).caveats, []);
  const caveats = transferThresholds({ annual_sensitive_count: '500', count_period: 'annual_estimate' }).caveats;
  assert.equal(caveats.length, 3);
  assert.match(caveats[0], /全年估算/);
  assert.equal(transferThresholds({ annual_non_sensitive_count: '5000', ciio_status: 'not_ciio', important_data_status: 'not_important', count_period: 'current_year_cumulative' }).caveats.length, 0);
});

test('zero sensitive records do not land in the standard-contract zone', () => {
  const view = transferThresholds({ annual_non_sensitive_count: '3200', annual_sensitive_count: '0' });
  assert.deepEqual(view.scales.map((scale) => scale.key), ['general']);
  assert.equal(view.scales[0].zoneLabel, '免于申报');
  assert.equal(view.scales[0].nextGap, 96_800);
});
