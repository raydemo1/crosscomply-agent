import type { ReviewFacts } from '../types/api';

/**
 * 出境路径的人数门槛刻度。
 *
 * 门槛取自《促进和规范数据跨境流动规定》第五条、第七条、第八条，适用于
 * 非关键信息基础设施运营者、自当年 1 月 1 日起累计的人数：
 * - 一般个人信息（不含敏感）不满 10 万人：免于申报；
 * - 10 万以上不满 100 万人，或敏感个人信息不满 1 万人：标准合同或认证；
 * - 100 万人以上，或敏感个人信息 1 万人以上：申报安全评估。
 *
 * 刻度条各区间等宽、区间内线性，只展示人数落在哪个区间，帮助读者理解报告结论；路径本身仍以报告
 * 结论及其前提（重要数据、自贸区负面清单等）为准。
 */

export type ScaleTone = 1 | 2 | 3;

export interface ScaleZone {
  label: string;
  from: number;
  to: number;
  tone: ScaleTone;
  /** 区间在刻度条上的起止位置（0–1）。各区间等宽，区间内按人数线性分布。 */
  start: number;
  end: number;
}

export interface ThresholdScale {
  key: 'general' | 'sensitive';
  label: string;
  value: number;
  max: number;
  zones: ScaleZone[];
  ticks: Array<{ value: number; label: string; at: number }>;
  /** 本案人数在刻度条上的位置（0–1）。 */
  marker: number;
  /** 本案人数所在区间的名称。 */
  zoneLabel: string;
  /** 距下一档门槛还差的人数；已在最高档时为 null。 */
  nextGap: number | null;
  nextLabel: string | null;
}

export interface ThresholdView {
  scales: ThresholdScale[];
  /** 不按人数门槛判断时的原因（CIIO 或重要数据）。 */
  blockedReason: string | null;
  /** 刻度成立的前提尚未确认时的提示（统计口径、CIIO 身份、重要数据）。 */
  caveats: string[];
}

const EXEMPT = '免于申报';
const CONTRACT = '标准合同 / 认证';
const ASSESSMENT = '安全评估';

export function parseHeadcount(raw: unknown): number | null {
  if (typeof raw === 'number') return Number.isFinite(raw) && raw >= 0 ? Math.round(raw) : null;
  if (typeof raw !== 'string') return null;
  const text = raw.replace(/[,，\s人约]/g, '');
  if (!text) return null;
  const wan = text.endsWith('万');
  const value = Number(wan ? text.slice(0, -1) : text);
  if (!Number.isFinite(value) || value < 0) return null;
  return Math.round(wan ? value * 10_000 : value);
}

/** 10000 → "1万"，1000000 → "100万"，8000 → "8,000"。 */
export function formatHeadcount(value: number): string {
  if (value >= 10_000 && value % 10_000 === 0) return `${value / 10_000}万`;
  return value.toLocaleString('en-US');
}

function roundUp(value: number, step: number): number {
  return Math.ceil(value / step) * step;
}

function scaleFor(
  key: ThresholdScale['key'],
  label: string,
  value: number,
  boundaries: Array<{ at: number; label: string }>,
  zoneLabels: string[],
  tones: ScaleTone[],
  minMax: number,
  step: number,
): ThresholdScale {
  const max = Math.max(minMax, roundUp(value * 1.15, step));
  const edges = [0, ...boundaries.map((b) => b.at), max];
  const share = 1 / zoneLabels.length;
  const zones = zoneLabels.map((zoneLabel, index) => ({
    label: zoneLabel, from: edges[index], to: edges[index + 1], tone: tones[index],
    start: index * share, end: (index + 1) * share,
  }));
  const zoneIndex = Math.max(0, zones.findIndex((zone) => value >= zone.from && value < zone.to));
  const index = value >= max ? zones.length - 1 : zoneIndex;
  const zone = zones[index];
  const within = Math.min(1, Math.max(0, (value - zone.from) / (zone.to - zone.from)));
  const next = boundaries[index];
  return {
    key,
    label,
    value,
    max,
    zones,
    ticks: boundaries.map((b, i) => ({ value: b.at, label: formatHeadcount(b.at), at: (i + 1) * share })),
    marker: zone.start + within * share,
    zoneLabel: zones[index].label,
    nextGap: next ? next.at - value : null,
    nextLabel: next ? zoneLabels[index + 1] : null,
  };
}

const generalScale = (value: number) => scaleFor('general', '非敏感个人信息', value,
  [{ at: 100_000, label: '10万' }, { at: 1_000_000, label: '100万' }],
  [EXEMPT, CONTRACT, ASSESSMENT], [1, 2, 3], 1_200_000, 100_000);
const sensitiveScale = (value: number) => scaleFor('sensitive', '敏感个人信息', value,
  [{ at: 10_000, label: '1万' }],
  [CONTRACT, ASSESSMENT], [2, 3], 15_000, 1_000);

export function buildRangeScale(lower: number, upper: number, sensitiveLower: number, sensitiveUpper: number, ciioAny = false) {
  const band = (general: number, sensitive: number) => general >= 1_000_000 || sensitive >= 10_000
    ? ASSESSMENT : general >= 100_000 || sensitive > 0 ? CONTRACT : EXEMPT;
  const withLower = (scale: ThresholdScale, minimum: number) => {
    const zone = scale.zones.find(({ to }) => minimum < to) ?? scale.zones[scale.zones.length - 1];
    return { ...scale, lower: minimum, lowerMarker: zone.start + (zone.end - zone.start) * (minimum - zone.from) / (zone.to - zone.from) };
  };
  return {
    lowerBand: band(lower, sensitiveLower),
    upperBand: band(upper, sensitiveUpper),
    scales: [withLower(generalScale(upper), lower), ...(sensitiveUpper > 0 ? [withLower(sensitiveScale(sensitiveUpper), sensitiveLower)] : [])],
    warning: ciioAny ? '存在 CIIO 身份记录，人数门槛可能不适用。' : null,
  };
}

export function transferThresholds(facts: Partial<ReviewFacts> | null | undefined): ThresholdView {
  const empty: ThresholdView = { scales: [], blockedReason: null, caveats: [] };
  if (!facts) return empty;
  if (facts.ciio_status === 'ciio') {
    return { ...empty, blockedReason: '关键信息基础设施运营者向境外提供个人信息，不论人数均须申报安全评估。' };
  }
  if (facts.important_data_status === 'important') {
    return { ...empty, blockedReason: '涉及已认定的重要数据，须申报安全评估，不按人数门槛判断。' };
  }
  const general = parseHeadcount(facts.annual_non_sensitive_count);
  const sensitive = parseHeadcount(facts.annual_sensitive_count);
  const scales: ThresholdScale[] = [];
  if (general !== null) {
    scales.push({ ...generalScale(general), label: '一般个人信息' });
  }
  // 敏感个人信息为 0 时不触发「不满 1 万人」这一档，不画刻度。
  if (sensitive !== null && sensitive > 0) {
    scales.push(sensitiveScale(sensitive));
  }
  const caveats: string[] = [];
  if (scales.length) {
    if (facts.count_period === 'annual_estimate') caveats.push('人数为全年估算，门槛按当年累计计算，实际累计后需复核。');
    else if (facts.count_period !== 'current_year_cumulative') caveats.push('人数统计口径尚未确认为当年累计，门槛位置仅供参考。');
    if (facts.ciio_status !== 'not_ciio') caveats.push('关键信息基础设施运营者身份尚未确认；如被认定，不论人数均须申报安全评估。');
    if (facts.important_data_status !== 'not_important') caveats.push('是否涉及重要数据尚未确认；如涉及，须申报安全评估。');
  }
  return { scales, blockedReason: null, caveats };
}
