from dataclasses import asdict
from types import SimpleNamespace

from law_agent.evals.arms.crosscomply import enqueue_eval_task
from law_agent.review.dates import business_today
from law_agent.review.enterprise_store import InMemoryEnterpriseStore
from law_agent.review.portfolio import (
    PortfolioContribution,
    PortfolioSummary,
    _contribution,
    band_for,
    entity_key,
    fingerprint_summary,
    parse_count,
    portfolio_ledger,
)


def test_normalization_count_and_band_boundaries():
    assert entity_key(" 上海某某（中国）ＡＢ有限公司 ") == entity_key("上海某某(中国)ab有限公司")
    assert entity_key("　 \n") == ""
    assert parse_count("约12万") is None
    assert parse_count("-1") is None
    assert parse_count(" 100，000 ") == 100_000
    assert band_for(99_999, 0) == "exempt"
    assert band_for(100_000, 0) == band_for(0, 1) == "contract_or_cert"
    assert band_for(1_000_000, 0) == band_for(0, 10_000) == "assessment"


def test_contribution_uses_business_year_and_explicit_counts():
    year = business_today().year
    activity = {"id": "activity", "title": "业务", "counts_toward_total": True, "exclusion_note": ""}
    snapshot = SimpleNamespace(
        id="intake", case_id="case", created_at=f"{year - 1}-12-31T16:00:00+00:00",
        intake={"annual_non_sensitive_count": "120000", "annual_sensitive_count": "0",
                "count_period": "annual_estimate"},
    )
    assert _contribution(activity, snapshot, year).status == "counted"
    assert _contribution(activity, snapshot, year + 1).status == "stale_year"
    assert _contribution({**activity, "counts_toward_total": False}, snapshot, year).status == "excluded"
    snapshot.intake["annual_non_sensitive_count"] = "约12万"
    assert _contribution(activity, snapshot, year).status == "unparsed"


def test_eval_builds_frozen_snapshot_and_derived_refs():
    store = InMemoryEnterpriseStore()
    payload = asdict(fingerprint_summary(PortfolioSummary(
        "示例公司", "示例公司", 2026, False,
        [PortfolioContribution("transfer_example", "境外工单归档", "case_example", "intake_example", 2026,
                               50_000, 0, "current_year_cumulative", True, "counted")],
        50_000, 50_000, 0, 0, band_for(50_000, 0), band_for(50_000, 0),
    )))
    task = enqueue_eval_task(store, case_id="portfolio_case", files=[("材料.txt", "示例材料")],
                             intake={"business_entity": "示例公司"}, model_id="fake", portfolio=payload)
    snapshot = store.get_portfolio_snapshot(task.portfolio_snapshot_id)
    entries = portfolio_ledger(snapshot)
    assert snapshot.payload == payload
    assert all((row.source_type, row.status, row.source_ref) == ("transfer_register", "derived", snapshot.id) for row in entries)
    assert next(row.value for row in entries if row.field == "entity_other_non_sensitive_sum") == 50_000
    snapshot.payload["non_sensitive_sum"] = 0
    assert store.get_portfolio_snapshot(snapshot.id).payload["non_sensitive_sum"] == 50_000
