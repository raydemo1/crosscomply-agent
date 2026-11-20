"""Deterministic annual headcount bounds for approved transfer activities."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any, Literal

from law_agent.review.dates import BUSINESS_TIMEZONE
from law_agent.review.schemas import FactLedgerEntry

# 《促进和规范数据跨境流动规定》第五、七、八条
GENERAL_CONTRACT = 100_000
GENERAL_ASSESSMENT = 1_000_000
SENSITIVE_ASSESSMENT = 10_000
Band = Literal["exempt", "contract_or_cert", "assessment"]
BANDS = ("exempt", "contract_or_cert", "assessment")
APPROVED = ("approved", "conditionally_approved")


def entity_key(name: str) -> str:
    normalized = "".join(
        unicodedata.normalize("NFKC", char) if unicodedata.east_asian_width(char) == "F" else char
        for char in name
    )
    return "".join(normalized.casefold().split())


def parse_count(value: str | None) -> int | None:
    if not value:
        return None
    try:
        count = int(value.replace(",", "").replace("，", "").strip())
    except ValueError:
        return None
    return count if count >= 0 else None


def band_for(non_sensitive: int, sensitive: int) -> Band:
    if non_sensitive >= GENERAL_ASSESSMENT or sensitive >= SENSITIVE_ASSESSMENT:
        return "assessment"
    if non_sensitive >= GENERAL_CONTRACT or sensitive > 0:
        return "contract_or_cert"
    return "exempt"


@dataclass(frozen=True)
class PortfolioContribution:
    activity_id: str
    activity_title: str
    baseline_case_id: str
    intake_snapshot_id: str
    snapshot_year: int
    non_sensitive: int | None
    sensitive: int | None
    count_period: str
    counts_toward_total: bool
    status: Literal["counted", "stale_year", "unparsed", "excluded"]
    exclusion_note: str = ""


@dataclass(frozen=True)
class PortfolioSummary:
    entity_key: str
    entity_name: str
    year: int
    ciio_any: bool
    contributions: list[PortfolioContribution]
    non_sensitive_sum: int
    non_sensitive_max: int
    sensitive_sum: int
    sensitive_max: int
    band_upper: Band
    band_lower: Band
    other_activity_count: int = 0
    counted_other_count: int = 0
    incomplete_other_count: int = 0
    scope_limited: bool = False
    fingerprint: str = ""


@dataclass(frozen=True)
class PortfolioSnapshot:
    id: str
    case_id: str
    entity_key: str
    year: int
    excluded_activity_id: str | None
    payload: dict[str, Any]
    fingerprint: str
    created_at: str


def approved_context(case_id, *, cases, enterprise, governance):
    case = cases.get_case(case_id)
    approval = governance.get_latest_approval(case_id)
    task = enterprise.get_task(approval.task_id) if approval and approval.case_id == case_id else None
    snapshot = enterprise.get_intake_snapshot(task.intake_snapshot_id) if task else None
    if (case is None or case["status"] not in APPROVED or approval is None
            or approval.status not in APPROVED or task is None or task.status != "succeeded"
            or snapshot is None or task.case_id != case_id or snapshot.case_id != case_id
            or snapshot.material_snapshot_id != task.material_snapshot_id):
        raise ValueError("此案件缺少已签署的审批结果或已确认的业务信息")
    return case, approval, snapshot


def _contribution(activity, snapshot, year):
    intake = snapshot.intake
    snapshot_year = datetime.fromisoformat(snapshot.created_at).astimezone(BUSINESS_TIMEZONE).year
    period = intake.get("count_period", "unknown")
    general = parse_count(intake.get("annual_non_sensitive_count"))
    sensitive = parse_count(intake.get("annual_sensitive_count"))
    excluded = not activity["counts_toward_total"]
    status = (
        "excluded" if excluded else "stale_year" if snapshot_year != year
        else "unparsed" if period not in {"current_year_cumulative", "annual_estimate"}
        or general is None or sensitive is None else "counted"
    )
    return PortfolioContribution(
        activity["id"], activity["title"], snapshot.case_id, snapshot.id, snapshot_year,
        general, sensitive, period, not excluded, status, activity["exclusion_note"],
    )


def fingerprint_summary(summary: PortfolioSummary, excluded_activity_id: str | None = None) -> PortfolioSummary:
    payload = asdict(summary)
    payload.pop("fingerprint")
    encoded = json.dumps([payload, excluded_activity_id], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return replace(summary, fingerprint=hashlib.sha256(encoded.encode("utf-8")).hexdigest())


def crosses_threshold(summary: PortfolioSummary) -> bool:
    return BANDS.index(summary.band_upper) > max(
        (BANDS.index(band_for(row.non_sensitive or 0, row.sensitive or 0))
         for row in summary.contributions if row.status == "counted"),
        default=0,
    )


class Portfolio:
    def __init__(self, transfers, cases, enterprise, governance):
        self.transfers = transfers
        self.cases = cases
        self.enterprise = enterprise
        self.governance = governance

    def _rows(self, year, *, key=None):
        from law_agent.review.transfer_store import latest_approved_case

        for activity in self.transfers.list_all():
            reviews = self.transfers.reviews(activity["id"])
            baseline = latest_approved_case(activity, reviews)
            designation = next((row for row in reviews if row["case_id"] == baseline), activity)
            if not designation["entity_key"] or (key is not None and key != designation["entity_key"]):
                continue
            try:
                _, _, snapshot = approved_context(
                    baseline, cases=self.cases, enterprise=self.enterprise, governance=self.governance,
                )
            except ValueError:
                continue
            yield designation, activity, snapshot, _contribution(activity, snapshot, year)

    @staticmethod
    def _summarize(key, name, year, rows, *, owner=None, excluded=None, scope_limited=False):
        ordered = sorted(rows, key=lambda row: row[3].activity_id)
        counted = [row[3] for row in ordered if row[3].status == "counted"]
        general = [row.non_sensitive or 0 for row in counted]
        sensitive = [row.sensitive or 0 for row in counted]
        def own(row):
            return owner is None or row[1]["owner_id"] == owner

        return fingerprint_summary(PortfolioSummary(
            key, name, year, any(row[2].intake.get("ciio_status") == "ciio" for row in ordered),
            [row[3] for row in ordered if own(row)],
            sum(general), max(general, default=0), sum(sensitive), max(sensitive, default=0),
            band_for(sum(general), sum(sensitive)),
            band_for(max(general, default=0), max(sensitive, default=0)),
            sum(not own(row) for row in ordered),
            sum(not own(row) and row[3].status == "counted" for row in ordered),
            sum(not own(row) and row[3].status in {"stale_year", "unparsed"} for row in ordered),
            scope_limited,
        ), excluded)

    def summaries(self, user, year, *, key=None):
        groups = {}
        for row in self._rows(year, key=key):
            groups.setdefault(row[0]["entity_key"], []).append(row)
        owner = user.id if user.role == "requester" else None
        items = []
        for entity, rows in groups.items():
            visible = [row for row in rows if owner is None or row[1]["owner_id"] == owner]
            if visible:
                newest = max(visible, key=lambda row: (row[0]["created_at"], row[3].activity_id))
                items.append(self._summarize(entity, newest[0]["business_entity"], year, rows, owner=owner))
        return sorted(items, key=lambda item: (item.entity_name, item.entity_key))

    def freeze(self, case, intake_snapshot, today):
        identifier = case["id"]
        activity_id = self.transfers.for_case(identifier)
        designation = self.transfers.review_for_case(identifier) or (
            self.transfers.get(activity_id) if activity_id else None
        )
        name = designation["business_entity"] if designation else intake_snapshot.intake.get("business_entity", "")
        key = designation["entity_key"] if designation else entity_key(name)
        if not key:
            return None
        owner = case.get("owner_id") or case["created_by"]
        rows = list(self._rows(today.year, key=key))
        scope_limited = not any(row[1]["owner_id"] == owner for row in rows)
        summary = self._summarize(
            key, name, today.year,
            [] if scope_limited else [row for row in rows if row[1]["id"] != activity_id],
            owner=owner, excluded=activity_id, scope_limited=scope_limited,
        )
        return self.enterprise.create_portfolio_snapshot(identifier, asdict(summary), activity_id)

    def acked(self, key, year, band):
        return self.transfers.portfolio_acked(key, year, band)


def portfolio_ledger(snapshot: PortfolioSnapshot) -> list[FactLedgerEntry]:
    payload = snapshot.payload
    contributions = payload["contributions"]
    incomplete = [row["activity_title"] for row in contributions if row["status"] in {"stale_year", "unparsed"}]
    hidden = payload.get("incomplete_other_count", 0)
    if hidden:
        incomplete.append(f"另有 {hidden} 项业务待更新或核实人数")
    values = {
        "entity_other_activities_count": len(contributions) + payload.get("other_activity_count", 0),
        "entity_other_non_sensitive_sum": payload["non_sensitive_sum"],
        "entity_other_non_sensitive_max": payload["non_sensitive_max"],
        "entity_other_sensitive_sum": payload["sensitive_sum"],
        "entity_other_sensitive_max": payload["sensitive_max"],
        "entity_stale_or_unparsed_activities": incomplete,
    }
    if payload["ciio_any"]:
        values["entity_ciio_notice"] = "存在 CIIO 身份记录，人数门槛可能不适用"
    if payload.get("scope_limited"):
        values["entity_portfolio_scope"] = "案件负责人尚未登记该主体的业务，累计人数需另行核对，不得视为零"
    return [
        FactLedgerEntry(field=field, value=value, source_type="transfer_register", source_ref=snapshot.id, status="derived")
        for field, value in values.items()
    ]
