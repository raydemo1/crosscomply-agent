/**
 * EvalPage — evaluation result surface used by the admin governance console.
 *
 * Renders the latest cached evaluation summary as a product-grade dashboard:
 *
 *   - header with generation time + corpus info + refresh button
 *   - six core metric cards (Recall@3/5, MRR@10, abstention, second-retrieval,
 *     bad-case rate)
 *   - radar chart comparing six normalized dimensions across modes
 *   - per-mode bar chart of Recall@5 for every golden-set case
 *   - expandable case table (click a row to see actual / missing sources,
 *     bad reasons, risk level)
 *   - bad-case gallery with reasons and missing sources
 *
 * The page is read-only with respect to the backend — it only calls
 * `GET /api/eval/latest`. Evaluation itself must be triggered out-of-band
 * (e.g. via the CLI or `POST /api/eval/run`), so the dashboard stays focused
 * on inspecting results rather than running them.
 *
 * Charts are hand-drawn SVG (no chart library) to keep the bundle small and
 * the visual style consistent with the Trust & Authority design system.
 */
import { useCallback, useEffect, useMemo, useState } from 'react';
import { ApiError, getLatestEval } from '../api/client';
import type {
  CaseMetricResult,
  EvalRerankMode,
  EvalSummary,
  ModeMetrics,
} from '../types/api';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Format a 0-1 ratio as a percentage string with one decimal. */
function pct(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—';
  return `${(value * 100).toFixed(1)}%`;
}

/** Format a raw float to four decimals (for MRR). */
function fixed(value: number | null | undefined, digits = 4): string {
  if (value == null || Number.isNaN(value)) return '—';
  return value.toFixed(digits);
}

/** Shorten a case_id like "eval_cross_border_001" → "cross_border_001". */
function shortCaseId(caseId: string): string {
  return caseId.replace(/^eval_/, '');
}

/** Derive a category label from a case_id for grouping/coloring. */
function caseCategory(caseId: string): string {
  const id = caseId.replace(/^eval_/, '');
  if (id.startsWith('cross_border')) return 'cross_border';
  if (id.startsWith('standard_contract')) return 'standard_contract';
  if (id.startsWith('automotive')) return 'automotive';
  if (id.startsWith('abstain')) return 'abstention';
  if (id.startsWith('sensitive')) return 'sensitive';
  if (id.startsWith('classification') || id.startsWith('financial')) return 'classification';
  // regional cases
  if (/(shanghai|tianjin|hainan|beijing|zhejiang)/.test(id)) return 'regional';
  return 'other';
}

/** Chinese label for a case category. */
const CATEGORY_LABELS: Record<string, string> = {
  cross_border: '跨境评估',
  standard_contract: '标准合同',
  automotive: '智能网联汽车',
  regional: '地方清单',
  sensitive: '敏感信息',
  classification: '分类分级',
  abstention: '弃答',
  other: '其他',
};

/** Distinct color per category for the bar chart (navy / gold family + accents). */
const CATEGORY_COLORS: Record<string, string> = {
  cross_border: '#1e3a8a',
  standard_contract: '#1e40af',
  automotive: '#b45309',
  regional: '#0f766e',
  sensitive: '#7c2d12',
  classification: '#4338ca',
  abstention: '#64748b',
  other: '#94a3b8',
};

/** Ordered categories for legends. */
const CATEGORY_ORDER = [
  'cross_border',
  'standard_contract',
  'automotive',
  'regional',
  'sensitive',
  'classification',
  'abstention',
  'other',
];

/** Parse a mode key like "retrieval=service,review=llm" into a readable label. */
function modeLabel(modeKey: string): string {
  const parts = modeKey.split(',');
  const retrieval = parts.find((p) => p.startsWith('retrieval='))?.split('=')[1];
  const review = parts.find((p) => p.startsWith('review='))?.split('=')[1];
  const r = retrieval === 'service' ? 'Service' : 'Local';
  const v = review === 'llm' ? 'LLM' : review === 'multi_agent' ? 'Multi-Agent' : 'Local';
  return v;
}

/** Six radar dimensions, each normalized to 0-1. */
interface RadarDim {
  key: string;
  label: string;
  value: number;
}

function radarDims(m: ModeMetrics): RadarDim[] {
  const safe = (v: number | null | undefined) => (v == null || Number.isNaN(v) ? 0 : v);
  return [
    { key: 'r3', label: 'Recall@3', value: safe(m.mean_recall_at_3) },
    { key: 'r5', label: 'Recall@5', value: safe(m.mean_recall_at_5) },
    { key: 'mrr', label: 'MRR@10', value: safe(m.mean_mrr_at_10) },
    { key: 'abst', label: '拒答准确', value: safe(m.abstention_accuracy) },
    {
      key: 'cite',
      label: '引用合规',
      value:
        m.total_cases > 0
          ? 1 - m.total_citation_violations / m.total_cases
          : 1,
    },
    {
      key: 'good',
      label: '非坏例率',
      value: m.total_cases > 0 ? 1 - m.bad_case_count / m.total_cases : 1,
    },
  ];
}

// ---------------------------------------------------------------------------
// Radar chart (pure SVG)
// ---------------------------------------------------------------------------

interface RadarSeries {
  label: string;
  color: string;
  dims: RadarDim[];
}

function RadarChart({
  series,
  size = 320,
}: {
  series: RadarSeries[];
  size?: number;
}): JSX.Element {
  const cx = size / 2;
  const cy = size / 2;
  const radius = size / 2 - 56;
  const n = 6;
  const angle = (i: number) => (Math.PI * 2 * i) / n - Math.PI / 2;
  const point = (i: number, r: number): [number, number] => [
    cx + Math.cos(angle(i)) * r,
    cy + Math.sin(angle(i)) * r,
  ];

  // Grid rings at 0.25 / 0.5 / 0.75 / 1.0
  const rings = [0.25, 0.5, 0.75, 1.0];
  // Axis labels
  const labels = series[0]?.dims.map((d) => d.label) ?? [];

  return (
    <svg
      className="eval-radar"
      viewBox={`0 0 ${size} ${size}`}
      width={size}
      height={size}
      role="img"
      aria-label="评测雷达图"
    >
      {/* grid rings */}
      {rings.map((r) => {
        const pts = Array.from({ length: n }, (_, i) => point(i, radius * r).join(',')).join(' ');
        return (
          <polygon
            key={r}
            points={pts}
            fill="none"
            stroke="#cbd5e1"
            strokeWidth={1}
            opacity={0.6}
          />
        );
      })}
      {/* axes */}
      {Array.from({ length: n }, (_, i) => {
        const [x, y] = point(i, radius);
        return (
          <line
            key={i}
            x1={cx}
            y1={cy}
            x2={x}
            y2={y}
            stroke="#cbd5e1"
            strokeWidth={1}
          />
        );
      })}
      {/* axis labels */}
      {labels.map((label, i) => {
        const [x, y] = point(i, radius + 18);
        const anchor =
          Math.abs(x - cx) < 4 ? 'middle' : x > cx ? 'start' : 'end';
        return (
          <text
            key={i}
            x={x}
            y={y}
            textAnchor={anchor}
            dominantBaseline="middle"
            className="eval-radar__label"
            fontSize={11}
            fill="#475569"
          >
            {label}
          </text>
        );
      })}
      {/* series polygons */}
      {series.map((s) => {
        const pts = s.dims
          .map((d, i) => point(i, radius * Math.max(0, Math.min(1, d.value))).join(','))
          .join(' ');
        return (
          <g key={s.label}>
            <polygon
              points={pts}
              fill={s.color}
              fillOpacity={0.12}
              stroke={s.color}
              strokeWidth={2}
            />
            {s.dims.map((d, i) => {
              const [x, y] = point(i, radius * Math.max(0, Math.min(1, d.value)));
              return (
                <circle key={i} cx={x} cy={y} r={3} fill={s.color} />
              );
            })}
          </g>
        );
      })}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Horizontal bar chart — Recall@5 per case
// ---------------------------------------------------------------------------

interface BarRow {
  caseId: string;
  category: string;
  value: number | null;
  isBad: boolean;
}

function BarChart({ rows }: { rows: BarRow[] }): JSX.Element {
  const max = 1;
  return (
    <div className="eval-bars">
      {rows.map((row) => {
        const w = row.value != null ? `${(row.value / max) * 100}%` : '0%';
        const color = CATEGORY_COLORS[row.category] ?? '#94a3b8';
        return (
          <div className="eval-bars__row" key={row.caseId}>
            <div className="eval-bars__label" title={row.caseId}>
              {shortCaseId(row.caseId)}
            </div>
            <div className="eval-bars__track">
              <div
                className="eval-bars__fill"
                style={{ width: w, background: color }}
              />
              <span className="eval-bars__value">
                {row.isBad ? '⚠ ' : ''}
                {pct(row.value)}
              </span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Metric card
// ---------------------------------------------------------------------------

function MetricCard({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: 'good' | 'warn' | 'bad' | 'neutral';
}): JSX.Element {
  const toneClass = tone ? ` metric-card--${tone}` : '';
  return (
    <div className={`metric-card${toneClass}`}>
      <div className="metric-card__label">{label}</div>
      <div className="metric-card__value">{value}</div>
      {sub ? <div className="metric-card__sub">{sub}</div> : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Case table row (expandable)
// ---------------------------------------------------------------------------

function CaseRow({ item }: { item: CaseMetricResult }): JSX.Element {
  const [open, setOpen] = useState(false);
  const category = caseCategory(item.case_id);
  const color = CATEGORY_COLORS[category] ?? '#94a3b8';
  const recall5 = item.recall_at_5;
  const barWidth = recall5 != null ? `${recall5 * 100}%` : '0%';

  return (
    <div className={`case-row${open ? ' is-open' : ''}${item.is_bad_case ? ' is-bad' : ''}`}>
      <button
        type="button"
        className="case-row__head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <span className="case-row__toggle" aria-hidden="true">
          {open ? '▾' : '▸'}
        </span>
        <span
          className="case-row__cat-dot"
          style={{ background: color }}
          title={CATEGORY_LABELS[category] ?? category}
          aria-hidden="true"
        />
        <span className="case-row__id">{shortCaseId(item.case_id)}</span>
        <span className="case-row__cat">
          {CATEGORY_LABELS[category] ?? category}
        </span>
        <span className="case-row__bar" aria-hidden="true">
          <span
            className="case-row__bar-fill"
            style={{ width: barWidth, background: color }}
          />
        </span>
        <span className="case-row__num">{pct(recall5)}</span>
        <span className="case-row__num">{fixed(item.mrr_at_10)}</span>
        <span
          className={`case-row__flag ${
            item.abstention_correct ? 'case-row__flag--ok' : 'case-row__flag--bad'
          }`}
        >
          {item.abstention_correct ? '✓' : '✗'}
        </span>
        <span
          className={`case-row__flag ${
            item.second_retrieval_triggered
              ? 'case-row__flag--info'
              : 'case-row__flag--muted'
          }`}
          title={
            item.second_retrieval_triggered
              ? '系统触发了二次检索'
              : '系统未触发二次检索'
          }
        >
          {item.second_retrieval_triggered ? '已触发' : '未触发'}
        </span>
        <span className="case-row__num">{item.citation_violation_count}</span>
        <span
          className={`case-row__flag ${
            item.is_bad_case ? 'case-row__flag--bad' : 'case-row__flag--ok'
          }`}
        >
          {item.is_bad_case ? '坏例' : '通过'}
        </span>
      </button>
      {open ? (
        <div className="case-row__detail">
          <div className="case-row__detail-grid">
            <div>
              <div className="case-row__detail-label">风险等级</div>
              <div className="case-row__detail-value">
                {item.risk_level || '—'}
              </div>
            </div>
            <div>
              <div className="case-row__detail-label">Recall@3</div>
              <div className="case-row__detail-value">{pct(item.recall_at_3)}</div>
            </div>
            <div>
              <div className="case-row__detail-label">候选 Recall@50</div>
              <div className="case-row__detail-value">
                {pct(item.candidate_recall_at_50)}
              </div>
            </div>
            <div>
              <div className="case-row__detail-label">去重源 Recall@5</div>
              <div className="case-row__detail-value">
                {pct(item.distinct_source_recall_at_5)}
              </div>
            </div>
            <div>
              <div className="case-row__detail-label">重复源数@10</div>
              <div className="case-row__detail-value">
                {item.duplicate_source_count_at_10}
              </div>
            </div>
            <div>
              <div className="case-row__detail-label">二次检索触发</div>
              <div
                className="case-row__detail-value"
                style={{
                  color: item.second_retrieval_triggered
                    ? 'var(--color-primary)'
                    : '#94a3b8',
                  fontSize: '0.8125rem',
                }}
              >
                {item.second_retrieval_triggered ? '已触发' : '未触发'}
              </div>
            </div>
          </div>
          <div className="case-row__sources">
            <div className="case-row__sources-block">
              <div className="case-row__detail-label">实际命中来源</div>
              {item.actual_sources.length === 0 ? (
                <div className="case-row__empty">无</div>
              ) : (
                <ul className="case-row__source-list">
                  {item.actual_sources.map((s) => (
                    <li key={s}>{s}</li>
                  ))}
                </ul>
              )}
            </div>
            <div className="case-row__sources-block">
              <div className="case-row__detail-label">缺失来源</div>
              {item.missing_sources.length === 0 ? (
                <div className="case-row__empty">无</div>
              ) : (
                <ul className="case-row__source-list case-row__source-list--missing">
                  {item.missing_sources.map((s) => (
                    <li key={s}>{s}</li>
                  ))}
                </ul>
              )}
            </div>
          </div>
          {item.is_bad_case && item.bad_reasons.length > 0 ? (
            <div className="case-row__bad-reasons">
              <span className="case-row__detail-label">坏例原因：</span>
              {item.bad_reasons.join('、')}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export default function EvalPage(): JSX.Element {
  const [summary, setSummary] = useState<EvalSummary | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedMode, setSelectedMode] = useState<string | null>(null);
  const [rerankArm, setRerankArm] = useState<EvalRerankMode>('off');

  const fetchLatest = useCallback(
    async (arm: EvalRerankMode = rerankArm) => {
      setLoading(true);
      setError(null);
      try {
        const latest = await getLatestEval(arm);
        setSummary(latest);
        if (latest && latest.mode_metrics) {
          const keys = Object.keys(latest.mode_metrics);
          if (keys.length > 0) {
            // If current selectedMode is not in the new data, switch to the first available
            setSelectedMode((prev) =>
              prev && keys.includes(prev) ? prev : keys[0],
            );
          }
        }
      } catch (err) {
        setError(
          err instanceof ApiError
            ? err.message
            : err instanceof Error
              ? err.message
              : '读取评测结果失败',
        );
      } finally {
        setLoading(false);
      }
    },
    [rerankArm],
  );

  useEffect(() => {
    fetchLatest(rerankArm);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rerankArm]);

  const modeKeys = useMemo(
    () => (summary ? Object.keys(summary.mode_metrics) : []),
    [summary],
  );

  const activeMode = selectedMode ?? modeKeys[0] ?? null;
  const activeMetrics: ModeMetrics | null = activeMode
    ? summary?.mode_metrics[activeMode] ?? null
    : null;

  const activeCaseResults: CaseMetricResult[] = useMemo(() => {
    if (!summary || !activeMode) return [];
    return summary.all_case_results[activeMode] ?? [];
  }, [summary, activeMode]);

  const barRows: BarRow[] = useMemo(
    () =>
      activeCaseResults.map((c) => ({
        caseId: c.case_id,
        category: caseCategory(c.case_id),
        value: c.recall_at_5,
        isBad: c.is_bad_case,
      })),
    [activeCaseResults],
  );

  const radarSeries: RadarSeries[] = useMemo(() => {
    if (!summary) return [];
    return modeKeys.map((key, idx) => {
      const m = summary.mode_metrics[key];
      const colors = ['#1e3a8a', '#b45309', '#0f766e', '#7c2d12'];
      return {
        label: modeLabel(key),
        color: colors[idx % colors.length],
        dims: radarDims(m),
      };
    });
  }, [summary, modeKeys]);

  const badCases = summary?.bad_cases ?? [];

  return (
    <div className="eval-page">
      {/* ---------------- Header ---------------- */}
      <header className="eval-header">
        <div className="eval-header__left">
          <h1 className="eval-header__title">评测结果</h1>
        </div>
        <div className="eval-header__right">
          {summary ? (
            <div className="eval-header__meta">
              <div className="eval-header__meta-line">
                <span className="eval-header__meta-label">生成时间</span>
                <span className="eval-header__meta-value">
                  {summary.generated_at.replace('T', ' ').slice(0, 19)}
                </span>
              </div>
              <div className="eval-header__meta-line">
                <span className="eval-header__meta-label">语料库</span>
                <span className="eval-header__meta-value" title={summary.chunks_path}>
                  {summary.chunks_path.split(/[\\/]/).pop() ?? summary.chunks_path}
                </span>
              </div>
            </div>
          ) : (
            <div className="eval-header__meta-line">暂无缓存结果</div>
          )}
          <div className="eval-header__controls">
            <div className="eval-toolbar">
              {modeKeys.map((key) => {
                const active = key === activeMode;
                return (
                  <button
                    key={key}
                    type="button"
                    className={`eval-toolbar__chip${active ? ' is-active' : ''}`}
                    onClick={() => setSelectedMode(key)}
                    disabled={loading}
                    title={`切换到 ${modeLabel(key)} 模式`}
                  >
                    {modeLabel(key)}
                  </button>
                );
              })}
              <span className="eval-toolbar__divider" aria-hidden="true" />
              <button
                type="button"
                className={`eval-toolbar__chip${rerankArm === 'embedding' ? ' is-active' : ''}`}
                onClick={() => setRerankArm(rerankArm === 'off' ? 'embedding' : 'off')}
                disabled={loading}
                title="切换 Rerank 模式（Embedding 语义重排序）"
              >
                Rerank模式
              </button>
            </div>
            <button
              type="button"
              className="btn-secondary eval-header__refresh"
              onClick={() => fetchLatest(rerankArm)}
              disabled={loading}
            >
              {loading ? '刷新中…' : '刷新'}
            </button>
          </div>
        </div>
      </header>

      {/* ---------------- Error ---------------- */}
      {error ? (
        <section className="error-box" role="alert">
          <span className="error-box__mark" aria-hidden="true">!</span>
          <div>
            <div style={{ fontWeight: 700, marginBottom: '2px' }}>读取评测失败</div>
            <div style={{ wordBreak: 'break-word' }}>{error}</div>
            <div style={{ marginTop: 4, fontSize: '0.8125rem', color: '#64748b' }}>
              请先运行评测任务。
            </div>
          </div>
        </section>
      ) : null}

      {/* ---------------- Loading / empty ---------------- */}
      {loading && !summary ? (
        <section className="card state-block">
          <span
            className="spinner spinner--dark"
            style={{ margin: '0 auto var(--space-md)' }}
            aria-hidden="true"
          />
          <div className="state-block__title">正在读取评测结果…</div>
        </section>
      ) : null}

      {!loading && !summary && !error ? (
        <section className="card state-block">
          <div className="state-block__title">暂无评测结果</div>
          <div className="state-block__hint">请先运行评测任务。</div>
        </section>
      ) : null}

      {/* ---------------- Dashboard ---------------- */}
      {summary && activeMetrics ? (
        <>
          {/* Core metric cards */}
          <section className="eval-cards">
            <MetricCard
              label="Must-have Recall@5"
              value={pct(activeMetrics.mean_must_have_recall_at_5)}
              sub={`核心法律依据 · ${activeMetrics.must_have_case_count} 例`}
              tone={activeMetrics.mean_must_have_recall_at_5 >= 0.85 ? 'good' : 'warn'}
            />
            <MetricCard
              label="Recall@5"
              value={pct(activeMetrics.mean_recall_at_5)}
              sub={`全部预期源 · ${activeMetrics.source_bearing_case_count} 例`}
              tone={activeMetrics.mean_recall_at_5 >= 0.7 ? 'good' : 'warn'}
            />
            <MetricCard
              label="Optional coverage@5"
              value={pct(activeMetrics.mean_optional_coverage_at_5)}
              sub={`辅助参考 · ${activeMetrics.optional_supporting_case_count} 例`}
              tone={activeMetrics.mean_optional_coverage_at_5 >= 0.7 ? 'good' : 'warn'}
            />
            <MetricCard
              label="MRR@10"
              value={fixed(activeMetrics.mean_mrr_at_10)}
              tone={activeMetrics.mean_mrr_at_10 >= 0.5 ? 'good' : 'warn'}
            />
            <MetricCard
              label="拒答准确率"
              value={pct(activeMetrics.abstention_accuracy)}
              tone={activeMetrics.abstention_accuracy >= 0.9 ? 'good' : 'bad'}
            />
            <MetricCard
              label="坏例率"
              value={pct(
                activeMetrics.total_cases > 0
                  ? activeMetrics.bad_case_count / activeMetrics.total_cases
                  : 0,
              )}
              sub={`${activeMetrics.bad_case_count} / ${activeMetrics.total_cases}`}
              tone={
                activeMetrics.bad_case_count === 0
                  ? 'good'
                  : activeMetrics.bad_case_count <= 2
                    ? 'warn'
                    : 'bad'
              }
            />
          </section>

          {/* Radar chart */}
          <section className="card">
            <div className="section-title">能力雷达图</div>
            <div className="eval-radar-wrap">
              <RadarChart series={radarSeries} />
            </div>
            {radarSeries.length > 1 ? (
              <div className="eval-legend">
                {radarSeries.map((s) => (
                  <span key={s.label} className="eval-legend__item">
                    <span
                      className="eval-legend__dot"
                      style={{ background: s.color }}
                      aria-hidden="true"
                    />
                    {s.label}
                  </span>
                ))}
              </div>
            ) : null}
          </section>

          {/* Per-case bar chart */}
          <section className="card">
            <div className="section-title">各用例 Recall@5</div>
            <BarChart rows={barRows} />
            <div className="eval-legend eval-legend--bar">
              {CATEGORY_ORDER.filter((c) =>
                barRows.some((r) => r.category === c),
              ).map((c) => (
                <span key={c} className="eval-legend__item">
                  <span
                    className="eval-legend__dot"
                    style={{ background: CATEGORY_COLORS[c] }}
                    aria-hidden="true"
                  />
                  {CATEGORY_LABELS[c] ?? c}
                </span>
              ))}
            </div>
          </section>

          {/* Case table */}
          <section className="card eval-cases">
            <div className="section-title">用例明细</div>
            <p className="eval-section-hint">点击用例查看来源与坏例原因。</p>
            <div className="case-row case-row--head">
              <span className="case-row__toggle" aria-hidden="true" />
              <span className="case-row__cat-dot" aria-hidden="true" />
              <span className="case-row__id">用例</span>
              <span className="case-row__cat">分类</span>
              <span className="case-row__bar">Recall@5</span>
              <span className="case-row__num">R@5</span>
              <span className="case-row__num">MRR</span>
              <span className="case-row__flag">拒答</span>
              <span className="case-row__flag">二次</span>
              <span className="case-row__num">违规</span>
              <span className="case-row__flag">状态</span>
            </div>
            {activeCaseResults.map((c) => (
              <CaseRow key={c.case_id} item={c} />
            ))}
          </section>

          {/* Bad cases */}
          <section className="card">
            <div className="section-title">
              坏例 ({badCases.length})
            </div>
            {badCases.length === 0 ? (
              <div className="state-block__hint">本次评测没有坏例。</div>
            ) : (
              <div className="eval-bad-grid">
                {badCases.map((c) => (
                  <div key={c.case_id} className="eval-bad-card">
                    <div className="eval-bad-card__head">
                      <span
                        className="eval-bad-card__dot"
                        style={{
                          background:
                            CATEGORY_COLORS[caseCategory(c.case_id)] ?? '#94a3b8',
                        }}
                        aria-hidden="true"
                      />
                      <span className="eval-bad-card__id">
                        {shortCaseId(c.case_id)}
                      </span>
                      <span className="eval-bad-card__cat">
                        {CATEGORY_LABELS[caseCategory(c.case_id)] ?? caseCategory(c.case_id)}
                      </span>
                    </div>
                    <div className="eval-bad-card__reasons">
                      {c.bad_reasons.length > 0
                        ? c.bad_reasons.join('、')
                        : '未标记原因'}
                    </div>
                    {c.missing_sources.length > 0 ? (
                      <div className="eval-bad-card__missing">
                        <span>缺失来源：</span>
                        {c.missing_sources.join('、')}
                      </div>
                    ) : null}
                    <div className="eval-bad-card__meta">
                      R@5 {pct(c.recall_at_5)} · MRR {fixed(c.mrr_at_10)} · 违规 {c.citation_violation_count}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </section>
        </>
      ) : null}
    </div>
  );
}
