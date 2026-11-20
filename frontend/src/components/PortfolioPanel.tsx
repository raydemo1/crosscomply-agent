import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ChevronDown } from 'lucide-react';
import { acknowledgePortfolio, getPortfolioSnapshot, listPortfolios } from '../api/client';
import type { PortfolioSnapshot, PortfolioSummary } from '../types/operations';
import { buildRangeScale, formatHeadcount } from '../utils/transferThresholds';
import '../styles/report-figures.css';

const statuses = {
  counted: '已计入', stale_year: '需更新本年度人数', unparsed: '人数或统计口径无法确认', excluded: '已排除',
};

function PortfolioBounds({ item }: { item: PortfolioSummary }) {
  const { lowerBand, upperBand, scales, warning } = buildRangeScale(
    item.non_sensitive_max, item.non_sensitive_sum, item.sensitive_max, item.sensitive_sum, item.ciio_any,
  );
  const incomplete = item.incomplete_other_count + item.contributions.filter(({ status }) =>
    status === 'stale_year' || status === 'unparsed').length;
  const hasCounts = item.contributions.some(({ status }) => status === 'counted') || item.counted_other_count > 0;
  if (item.scope_limited) return <p className="ops-secondary">案件负责人未登记该主体的业务，需另行核对同主体累计人数。</p>;
  return <>
    {hasCounts ? <>
      {scales.map((scale) => {
        const lower = formatHeadcount(scale.lower), upper = formatHeadcount(scale.value);
        return <div className="threshold-row" key={scale.key}>
          <div className="threshold-row__label"><strong>{scale.label}</strong></div>
          <div className="threshold-row__chart">
            <div className="threshold-row__bar" role="img" aria-label={`单项最高 ${lower} 人；合计·未去重 ${upper} 人`}>
              {scale.zones.map((zone) => <span key={zone.label} className={`threshold-zone threshold-zone--${zone.tone}`}
                style={{ left: `${zone.start * 100}%`, width: `calc(${(zone.end - zone.start) * 100}% - 2px)` }}>{zone.label}</span>)}
              <span className="portfolio-marker" style={{ left: `${scale.lowerMarker * 100}%` }} aria-hidden="true" />
              <span className="portfolio-marker portfolio-marker--upper" style={{ left: `${scale.marker * 100}%` }} aria-hidden="true" />
            </div>
            <p className="threshold-row__caption">单项最高 {lower} 人 · 合计（未去重）{upper} 人</p>
          </div>
        </div>;
      })}
      <p className="threshold-scales__note">{lowerBand === upperBand ? `已计入业务的人数落在「${lowerBand}」档。`
        : `已计入业务去重后可能处于「${lowerBand}」至「${upperBand}」档，需确认跨业务去重人数。`}</p>
    </> : <p className="ops-secondary">暂无已计入的人数记录。</p>}
    {incomplete > 0 ? <p className="ops-secondary">{incomplete} 项业务需更新或核对人数，未计入合计。</p> : null}
    {warning ? <p className="ops-secondary">{warning}</p> : null}
    {item.contributions.some(({ count_period, status }) => count_period === 'annual_estimate' && status === 'counted')
      ? <p className="ops-secondary">包含全年估算，需按实际累计人数复核。</p> : null}
  </>;
}

export function PortfolioDirectory({ selected }: { selected: string | null }) {
  const [items, setItems] = useState<PortfolioSummary[] | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    void listPortfolios().then(({ items: rows }) => { if (active) setItems(rows); })
      .catch((reason: unknown) => { if (active) setError(reason instanceof Error ? reason.message : '无法读取主体累计'); });
    return () => { active = false; };
  }, []);
  if (error) return <p className="ops-error" role="alert">{error}</p>;
  if (items === null) return <p className="ops-empty">正在读取主体累计…</p>;
  if (!items.length) return <p className="ops-empty">暂无主体累计记录</p>;
  return <section className="ops-panel ops-history" aria-label="按主体查看">
    {items.map((item) => <details key={item.entity_key} className="ops-review" open={selected === item.entity_key || undefined}>
      <summary><strong>{item.entity_name}</strong><span className="ops-tag">{item.year} 年</span><ChevronDown size={17} aria-hidden="true" /></summary>
      <div className="ops-review__body">
        <PortfolioBounds item={item} />
        {item.contributions.length ? <table className="ops-diff">
          <thead><tr><th scope="col">业务</th><th scope="col">非敏感人数</th><th scope="col">敏感人数</th><th scope="col">统计状态</th></tr></thead>
          <tbody>{item.contributions.map((row) => <tr key={row.activity_id}>
            <th scope="row"><Link className="ops-link" to={`/cases/business/${row.activity_id}`}>{row.activity_title}</Link></th>
            <td>{row.non_sensitive === null ? '—' : formatHeadcount(row.non_sensitive)}</td>
            <td>{row.sensitive === null ? '—' : formatHeadcount(row.sensitive)}</td>
            <td>{statuses[row.status]}{row.exclusion_note ? ` · ${row.exclusion_note}` : ''}</td>
          </tr>)}</tbody>
        </table> : null}
        {item.other_activity_count > 0 ? <p className="ops-secondary">另有 {item.other_activity_count} 项同主体业务</p> : null}
        {item.threshold_alert ? <AckForm item={item} onConfirmed={() => setItems((current) => current?.map((row) =>
          row.entity_key === item.entity_key ? { ...row, threshold_alert: false } : row) ?? null)} /> : null}
      </div>
    </details>)}
  </section>;
}

function AckForm({ item, onConfirmed }: { item: PortfolioSummary; onConfirmed: () => void }) {
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  async function submit() {
    setBusy(true);
    setError('');
    try {
      await acknowledgePortfolio(item.entity_key, { year: item.year, band: item.band_upper, note: note.trim() });
      onConfirmed();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '确认失败，请重试');
    } finally {
      setBusy(false);
    }
  }
  return <form className="ops-impact ops-impact--form" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
    <h3>核对累计跨档提示</h3>
    <label className="ops-field">核对说明<textarea required rows={2} maxLength={1600} value={note} onChange={(event) => setNote(event.target.value)} /></label>
    {error ? <p className="ops-error" role="alert">{error}</p> : null}
    <button type="submit" className="btn-primary" disabled={busy || !note.trim()}>确认已核对</button>
  </form>;
}

export function FrozenPortfolio({ caseId, snapshotId }: { caseId: string; snapshotId: string }) {
  const [snapshot, setSnapshot] = useState<PortfolioSnapshot | null>(null);
  const [error, setError] = useState('');
  useEffect(() => {
    let active = true;
    void getPortfolioSnapshot(caseId).then((frozen) => {
      if (!active) return;
      if (frozen.id === snapshotId) setSnapshot(frozen);
      else setError('审查版本已更新，请刷新案件。');
    }).catch(() => { if (active) setError('无法读取主体累计快照。'); });
    return () => { active = false; };
  }, [caseId, snapshotId]);
  return <section className="case-facts__group" aria-label="同主体累计">
    <h3 className="case-facts__group-title">同主体累计</h3>
    {snapshot ? <><p className="ops-secondary">{snapshot.payload.entity_name} · {snapshot.payload.year} 年 · 不含本案</p>
      <PortfolioBounds item={snapshot.payload} />
      <p className="threshold-scales__note">审查时冻结，之后的业务变化不影响本报告。</p></>
      : <p className="ops-secondary" role="status">{error || '正在读取快照…'}</p>}
  </section>;
}
