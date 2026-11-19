import type { CSSProperties } from 'react';
import type { Citation, IssueKind, ReviewFacts, ReviewIssue, RiskLevel } from '../types/api';
import { ISSUE_KIND_LABELS, RISK_LABELS } from '../utils/display';
import { parseHeadcount, transferThresholds, type ThresholdScale } from '../utils/transferThresholds';
import '../styles/report-figures.css';

// ---------------------------------------------------------------------------
// 风险朱印：方框印章式的风险等级
// ---------------------------------------------------------------------------

const SEAL_GLYPH: Record<RiskLevel, [string, string]> = {
  high: ['高', '风险'],
  medium: ['中', '风险'],
  low: ['低', '风险'],
  insufficient_evidence: ['待', '证据'],
};

/** 风险朱印。size="sm" 为列表里的小印：只留一个字，配文字标签。 */
export function RiskSeal({ level, size = 'lg' }: { level: RiskLevel; size?: 'lg' | 'sm' }): JSX.Element {
  const [glyph, caption] = SEAL_GLYPH[level] ?? SEAL_GLYPH.insufficient_evidence;
  const label = RISK_LABELS[level] ?? RISK_LABELS.insufficient_evidence;
  if (size === 'sm') {
    return <span className={`risk-mark risk-seal--${level}`} title={label}>
      <span className="risk-mark__glyph" aria-hidden="true">{glyph}</span>
      <span className="risk-mark__label">{label}</span>
    </span>;
  }
  return <div className={`risk-seal risk-seal--${level}`} role="img" aria-label={`风险等级：${label}`}>
    <span className="risk-seal__glyph" aria-hidden="true">{glyph}</span>
    <span className="risk-seal__caption" aria-hidden="true">{caption}</span>
  </div>;
}

// ---------------------------------------------------------------------------
// 关键数字与出境路径门槛
// ---------------------------------------------------------------------------

const PERIOD_LABELS: Record<NonNullable<ReviewFacts['count_period']>, string> = {
  current_year_cumulative: '当年累计',
  annual_estimate: '全年估算',
  other: '其他口径',
  unknown: '口径未定',
};
const STATUS_SHORT: Record<string, { text: string; pending: boolean }> = {
  not_ciio: { text: '否', pending: false },
  ciio: { text: '是', pending: false },
  not_important: { text: '否', pending: false },
  important: { text: '是', pending: false },
  under_review: { text: '核实中', pending: true },
  unknown: { text: '待核实', pending: true },
};

/** 材料明确不涉及个人信息或不出境时，人数门槛与本案无关。 */
function personalInfoOutOfScope(facts: Partial<ReviewFacts>): boolean {
  return facts.contains_personal_information === false || facts.cross_border_transfer === false;
}

export function KeyFigures({ facts }: { facts: Partial<ReviewFacts> | null | undefined }): JSX.Element | null {
  if (!facts) return null;
  const general = parseHeadcount(facts.annual_non_sensitive_count);
  const sensitive = parseHeadcount(facts.annual_sensitive_count);
  const period = PERIOD_LABELS[facts.count_period ?? 'unknown'] ?? PERIOD_LABELS.unknown;
  const ciio = STATUS_SHORT[facts.ciio_status ?? 'unknown'] ?? STATUS_SHORT.unknown;
  const important = STATUS_SHORT[facts.important_data_status ?? 'unknown'] ?? STATUS_SHORT.unknown;
  // 已须安全评估（CIIO 或重要数据）时，人数不再决定路径，缺失也不必标为待核实。
  const countsMatter = !transferThresholds(facts).blockedReason;
  const missing = countsMatter ? '未提供' : '—';
  const counts = personalInfoOutOfScope(facts) ? [] : [
    { label: `一般个人信息 · ${period}`, value: general === null ? missing : general.toLocaleString('en-US'), numeric: general !== null, pending: general === null && countsMatter },
    { label: `敏感个人信息 · ${period}`, value: sensitive === null ? (facts.sensitive_personal_info === false ? '不涉及' : missing) : sensitive.toLocaleString('en-US'), numeric: sensitive !== null, pending: sensitive === null && countsMatter && facts.sensitive_personal_info !== false },
  ];
  const items = [
    ...counts,
    { label: '关键信息基础设施运营者', value: ciio.text, numeric: false, pending: ciio.pending },
    { label: '重要数据', value: important.text, numeric: false, pending: important.pending },
  ];
  // 一项都没有确认时，这条数字带只会重复「待核实事项」，不显示。
  if (items.every((item) => item.pending)) return null;
  return <dl className="key-figures" style={{ '--key-figures-columns': items.length } as CSSProperties} aria-label="决定出境路径的关键事实">
    {items.map((item) => <div key={item.label}>
      <dt>{item.label}</dt>
      <dd className={(item.numeric ? 'is-numeric' : '') + (item.pending ? ' is-pending' : '')}>{item.value}</dd>
    </div>)}
  </dl>;
}

function pct(value: number, max: number): string {
  return `${Math.min(100, Math.max(0, (value / max) * 100)).toFixed(2)}%`;
}

/** 数值标签贴近两端时改为左/右对齐，避免越出刻度条。 */
function edgeAlign(at: number): string {
  if (at < 0.06) return 'translateX(-2px)';
  if (at > 0.94) return 'translateX(calc(-100% + 2px))';
  return 'translateX(-50%)';
}

function ScaleRow({ scale }: { scale: ThresholdScale }): JSX.Element {
  const count = scale.value.toLocaleString('en-US');
  const caption = `${scale.label} ${count} 人，落在「${scale.zoneLabel}」区间`
    + (scale.nextGap !== null && scale.nextLabel ? `，距「${scale.nextLabel}」门槛还差 ${scale.nextGap.toLocaleString('en-US')} 人。` : '，已达到最高一档。');
  return <div className="threshold-row">
    <div className="threshold-row__label"><strong>{scale.label}</strong><span>{scale.zoneLabel}</span></div>
    <div className="threshold-row__chart">
      <div className="threshold-row__value" style={{ left: pct(scale.marker, 1), transform: edgeAlign(scale.marker) }}>{count}</div>
      <div className="threshold-row__bar" role="img" aria-label={caption} title={caption}>
        {scale.zones.map((zone) => <span key={zone.label} className={`threshold-zone threshold-zone--${zone.tone}`} style={{ left: pct(zone.start, 1), width: `calc(${pct(zone.end - zone.start, 1)} - 2px)` }}>
          {zone.label}
        </span>)}
        <span className="threshold-row__marker" style={{ left: pct(scale.marker, 1) }} aria-hidden="true" />
      </div>
      <div className="threshold-row__ticks" aria-hidden="true">
        <span style={{ left: 0, transform: 'none' }}>0</span>
        {scale.ticks.map((tick) => <span key={tick.value} style={{ left: pct(tick.at, 1) }}>{tick.label}人</span>)}
      </div>
      <p className="threshold-row__caption">{caption}</p>
    </div>
  </div>;
}

export function ThresholdScales({ facts }: { facts: Partial<ReviewFacts> | null | undefined }): JSX.Element | null {
  const view = transferThresholds(facts);
  if (view.blockedReason) {
    const steps: Array<{ label: string; tone: 1 | 2 | 3 }> = [
      { label: '免于申报', tone: 1 }, { label: '标准合同 / 认证', tone: 2 }, { label: '安全评估', tone: 3 },
    ];
    return <section className="threshold-scales" aria-label="出境路径门槛">
      <h3 className="threshold-scales__title">出境路径门槛</h3>
      <ol className="path-ladder" aria-label={`适用路径：安全评估。${view.blockedReason}`}>
        {steps.map((step) => <li key={step.label} className={`threshold-zone--${step.tone}` + (step.tone === 3 ? ' is-current' : '')} aria-current={step.tone === 3 ? 'step' : undefined}>{step.label}</li>)}
      </ol>
      <p className="threshold-row__caption">{view.blockedReason}</p>
    </section>;
  }
  if (!view.scales.length) {
    if (!facts || personalInfoOutOfScope(facts)) return null;
    const steps: Array<{ label: string; tone: 1 | 2 | 3; rule: string }> = [
      { label: '免于申报', tone: 1, rule: '一般个人信息不满 10 万人' },
      { label: '标准合同 / 认证', tone: 2, rule: '10 万至 100 万人，或敏感个人信息不满 1 万人' },
      { label: '安全评估', tone: 3, rule: '100 万人以上，或敏感个人信息 1 万人以上' },
    ];
    return <section className="threshold-scales" aria-label="出境路径门槛">
      <h3 className="threshold-scales__title">出境路径门槛</h3>
      <ol className="path-ladder path-ladder--reference">
        {steps.map((step) => <li key={step.label} className={`threshold-zone--${step.tone}`}><strong>{step.label}</strong><span>{step.rule}</span></li>)}
      </ol>
      <p className="threshold-row__caption">个人信息人数尚未提供，暂无法判断本案落在哪一档；补充自当年 1 月 1 日起的累计人数后即可定位。</p>
      <p className="threshold-scales__note">门槛依《促进和规范数据跨境流动规定》，适用于非关键信息基础设施运营者；如被认定为关键信息基础设施运营者或涉及重要数据，不论人数均须申报安全评估。</p>
    </section>;
  }
  const tones = new Set(view.scales.flatMap((scale) => scale.zones.map((zone) => zone.tone)));
  return <section className="threshold-scales" aria-label="出境路径门槛">
    <div className="threshold-scales__head">
      <h3 className="threshold-scales__title">出境路径门槛</h3>
      <ul className="chart-legend" aria-label="图例">
        {tones.has(1) ? <li><span className="chart-swatch threshold-zone--1" />免于申报</li> : null}
        <li><span className="chart-swatch threshold-zone--2" />标准合同 / 认证</li>
        <li><span className="chart-swatch threshold-zone--3" />安全评估</li>
        <li><span className="chart-swatch chart-swatch--marker" />本案</li>
      </ul>
    </div>
    {view.scales.map((scale) => <ScaleRow scale={scale} key={scale.key} />)}
    <p className="threshold-scales__note">门槛依《促进和规范数据跨境流动规定》，按非关键信息基础设施运营者、自当年 1 月 1 日起累计人数计算；各区间等宽显示，最终路径以上方结论及其前提为准。</p>
    {view.caveats.length ? <ul className="threshold-scales__caveats" aria-label="刻度成立的前提">
      {view.caveats.map((caveat) => <li key={caveat}>{caveat}</li>)}
    </ul> : null}
  </section>;
}

// ---------------------------------------------------------------------------
// 主要问题构成、待办进度、法规依据构成
// ---------------------------------------------------------------------------

export const ISSUE_KIND_ORDER: IssueKind[] = ['legal_gap', 'missing_information', 'material_conflict'];
const ISSUE_KIND_CHART: Record<IssueKind, string> = {
  legal_gap: 'chart-1',
  missing_information: 'chart-2',
  material_conflict: 'chart-3',
};

export function issueKindChartClass(kind: IssueKind): string {
  return ISSUE_KIND_CHART[kind] ?? 'chart-1';
}

export function IssueMix({ issues }: { issues: readonly ReviewIssue[] }): JSX.Element | null {
  const counts = ISSUE_KIND_ORDER.map((kind) => ({ kind, count: issues.filter((issue) => issue.kind === kind).length })).filter((item) => item.count > 0);
  // 只有一类问题时构成条没有比较意义，由每条问题旁的类型色块说明即可。
  if (counts.length < 2) return null;
  const summary = counts.map((item) => `${ISSUE_KIND_LABELS[item.kind]} ${item.count} 项`).join('，');
  return <div className="issue-mix">
    <div className="stack-bar" role="img" aria-label={`问题构成：${summary}`} title={summary}>
      {counts.map((item) => <span key={item.kind} className={`stack-bar__seg ${issueKindChartClass(item.kind)}`} style={{ flexGrow: item.count }} />)}
    </div>
    <ul className="chart-legend">
      {counts.map((item) => <li key={item.kind}><span className={`chart-swatch ${issueKindChartClass(item.kind)}`} />{ISSUE_KIND_LABELS[item.kind]}<b>{item.count}</b></li>)}
    </ul>
  </div>;
}

export function SegmentProgress({ done, total, label }: { done: number; total: number; label: string }): JSX.Element | null {
  if (total <= 0) return null;
  const text = `${label}：已完成 ${done} 项，共 ${total} 项`;
  return <div className={'segment-progress' + (total > 12 ? ' segment-progress--continuous' : '')} role="img" aria-label={text} title={text}>
    {total > 12
      ? <span className="segment-progress__fill" style={{ width: pct(done, total) }} />
      : Array.from({ length: total }, (_, index) => <span key={index} className={index < done ? 'is-done' : undefined} />)}
  </div>;
}

export function SourceBars({ citations }: { citations: readonly Citation[] }): JSX.Element | null {
  const counts = new Map<string, number>();
  for (const citation of citations) {
    const raw = citation.title.trim();
    const title = !raw ? '其他来源' : raw.includes('《') ? raw : `《${raw}》`;
    counts.set(title, (counts.get(title) ?? 0) + 1);
  }
  const sorted = [...counts].sort((a, b) => b[1] - a[1]);
  if (sorted.length < 2) return null;
  // 来源过多时只列前五，其余合并，避免图表比条文清单还长。
  const rows = sorted.length > 6
    ? [...sorted.slice(0, 5), [`其他 ${sorted.length - 5} 部`, sorted.slice(5).reduce((sum, [, count]) => sum + count, 0)] as [string, number]]
    : sorted;
  const max = rows[0][1];
  return <div className="source-bars" role="list" aria-label="法规依据按来源统计">
    {rows.map(([title, count]) => <div className="source-bars__row" role="listitem" key={title} title={`${title}：${count} 条`}>
      <span className="source-bars__name">{title}</span>
      <span className="source-bars__track"><span className="source-bars__bar" style={{ width: `calc(${((count / max) * 100).toFixed(2)}% - 28px)` }} /><span className="source-bars__value">{count}</span></span>
    </div>)}
  </div>;
}
