"""SQL paths against an explicitly provisioned and migrated test database."""

import os
from datetime import datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from law_agent.evals.field import export_ready
from law_agent.review.case_store import PostgresCaseStore
from law_agent.review.dates import BUSINESS_TIMEZONE, business_today
from law_agent.review.enterprise_store import PostgresEnterpriseStore
from law_agent.review.eval_draft_store import DuplicateDraft, PostgresEvalDraftStore
from law_agent.review.eval_feedback import draft_inputs, validate_draft
from law_agent.review.governance_store import PostgresGovernanceStore
from law_agent.review.impact import RegulationImpact
from law_agent.review.impact_store import PostgresImpactStore
from law_agent.review.portfolio import Portfolio
from law_agent.review.transactions import case_transaction
from law_agent.review.transfer_store import PostgresTransferStore
from law_agent.review.user_admin import PostgresUserAdminStore
from tests.test_eval_drafts_api import sanitized
from tests.test_regulation_impact import index_case, make_change
from tests.test_work_inbox_transfers import signed_case

DSN = os.getenv("CROSSCOMPLY_TEST_PG_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="requires isolated migrated CROSSCOMPLY_TEST_PG_DSN")


def test_postgres_portfolio_impacts_and_draft_lifecycle(tmp_path):
    cases = PostgresCaseStore(DSN)
    cases.initialize()
    enterprise = PostgresEnterpriseStore(DSN)
    governance = PostgresGovernanceStore(DSN)
    transfers = PostgresTransferStore(DSN)
    records = PostgresImpactStore(DSN)
    impact = RegulationImpact(records, cases, enterprise, governance, transfers)
    drafts = PostgresEvalDraftStore(DSN)
    user = PostgresUserAdminStore(DSN).create_user(
        f"{uuid4().hex}@example.test", "测试审核人", uuid4().hex, "reviewer",
    )
    app = SimpleNamespace(state=SimpleNamespace(case_store=cases, enterprise_store=enterprise, governance_store=governance))
    counts = {"annual_non_sensitive_count": "60000", "annual_sensitive_count": "0", "count_period": "annual_estimate"}
    baseline = signed_case(app, user, intake=counts)
    today = business_today()
    entity = f"上海（中国）{uuid4().hex}"
    activity = transfers.register(origin_case_id=baseline["id"], owner_id=user.id, title="出境业务",
                                  business_entity=entity, next_review_on=today)
    prior = enterprise.get_latest_task(baseline["id"])
    intake = enterprise.get_intake_snapshot(prior.intake_snapshot_id)
    portfolio = Portfolio(transfers, cases, enterprise, governance)
    frozen = portfolio.freeze(baseline, intake, today)
    assert frozen.payload["contributions"] == []
    assert frozen.excluded_activity_id == activity
    assert portfolio.freeze(baseline, intake, today).id == frozen.id
    assert enterprise.get_portfolio_snapshot(frozen.id) == frozen
    queued = enterprise.enqueue_review_task(
        case_id=baseline["id"], material_snapshot_id=prior.material_snapshot_id,
        intake_snapshot_id=prior.intake_snapshot_id, model_id="fake", data_boundary_summary={},
        portfolio_snapshot_id=frozen.id,
    )
    assert enterprise.get_task(queued.id).portfolio_snapshot_id == frozen.id

    source, chunks, change = make_change()
    index_case(impact, baseline["id"], prior.id, source, chunks)
    event = records.record_change(change, created_by=user.id)
    assert impact.compute(event) == impact.compute(event) == 1
    flag, = records.flags(status="open", case_id=baseline["id"])
    assert records.open_blocking_count(baseline["id"], prior.id) == 1
    with case_transaction(cases, enterprise, baseline["id"]):
        updated = records.dispose(flag["id"], actor_id=user.id, note="已核对")
    assert updated["status"] == "dismissed"
    assert not records.open_blocking_count(baseline["id"], prior.id)

    payload = draft_inputs(baseline, prior, enterprise=enterprise, transfers=transfers)
    values = {"actor_id": user.id, "case_id": baseline["id"], "task_id": prior.id, "signals": [], **payload}
    draft = drafts.create(**values)
    with pytest.raises(DuplicateDraft):
        drafts.create(**values)
    edited = drafts.change(draft["id"], "edit", user.id, values=sanitized())
    assert edited["deidentified"]
    drafts.change(draft["id"], "ready", user.id, validate=lambda row: validate_draft(row, set())[1])
    assert export_ready(drafts, tmp_path / "field", set()) == 1
    assert drafts.get(draft["id"])["status"] == "exported"
    metrics = drafts.quality(datetime(2020, 1, 1, tzinfo=BUSINESS_TIMEZONE))
    assert metrics["conclusion_useful_rate"] is None
    assert metrics["regulation_impact"]["dismissed"] >= 1
