"""Deterministic matching of corpus changes against live review conclusions."""

import logging
from collections import defaultdict
from contextlib import nullcontext
from dataclasses import replace
from datetime import date

from law_agent.data.schemas import SourceRecord
from law_agent.review.dates import business_today
from law_agent.review.portfolio import APPROVED
from law_agent.review.retrieval.temporal import (
    _parsed,
    instrument_identity,
    version_effective_as_of,
)
from law_agent.review.transfer_store import latest_approved_case

LOG = logging.getLogger(__name__)
REASON_LABELS = {
    "cited_text_changed": "引用条文正文已变更",
    "source_removed": "法源已移除",
    "source_repealed": "法源已失效或修订",
    "superseded": "已有新版本生效",
    "superseded_upcoming": "新版本尚未生效",
}
ORDER = tuple(REASON_LABELS)


def citation_rows(task_id, case_id, result, chunks_by_id):
    for citation in (result.get("review_result") or {}).get("citations") or []:
        chunk = chunks_by_id.get(citation["chunk_id"])
        yield {
            "task_id": task_id, "case_id": case_id, "chunk_id": citation["chunk_id"],
            "source_id": citation["source_id"], "article_no": citation.get("article_no"),
            "title": citation["title"], "effective_date": citation.get("effective_date"),
            "can_cite_clause": citation["can_cite_clause"],
            "instrument_key": instrument_identity(chunk) if chunk else citation["title"].strip(),
        }


def match_reason(event, row, today: date):
    detail = event["detail"]
    if detail["metadata_only"]:
        return None
    source_id = event["source_id"]
    kind = event["change_type"]
    replacement = detail["after"]
    if row["source_id"] == source_id:
        if kind == "removed":
            return "source_removed", None
        if row["chunk_id"] in detail["removed_chunk_ids"]:
            return "cited_text_changed", None
        status = detail["status_changes"]
        law_status = status.get("law_status", {}).get("after")
        valid_to = _parsed(status.get("valid_to", {}).get("after"))
        if law_status in {"repealed", "amended"} or (valid_to and valid_to <= today):
            return "source_repealed", valid_to
        return None
    if not replacement or not event["instrument_key"] or event["instrument_key"] != row["instrument_key"]:
        return None
    source = SourceRecord.model_validate(replacement)
    if source.law_status in {"repealed", "amended"}:
        return None
    old_effective = _parsed(row["effective_date"])
    new_effective = _parsed(source.effective_date)
    if not old_effective or not new_effective or new_effective <= old_effective:
        return None
    if version_effective_as_of(source, today):
        return "superseded", new_effective
    if new_effective > today and version_effective_as_of(source, new_effective):
        return "superseded_upcoming", new_effective
    return None


class RegulationImpact:
    def __init__(self, records, cases, enterprise, governance, transfers):
        self.records = records
        self.cases = cases
        self.enterprise = enterprise
        self.governance = governance
        self.transfers = transfers

    def track_publication(self, service, source_id, mutation, *, job_id=None, actor_id=None, restoring=False):
        from law_agent.kb.admin import corpus_mutation_lock
        from law_agent.kb.change_detection import diff_source

        lock = corpus_mutation_lock() if hasattr(self.records, "dsn") else nullcontext()
        with lock:
            before = service.source_snapshot(source_id)
            result = mutation()
            try:
                change = diff_source(*before, *service.source_snapshot(source_id))
                if change is None:
                    return result
                if restoring and change.change_type == "added":
                    change = replace(change, change_type="restored")
                event = self.records.record_change(change, job_id=job_id, created_by=actor_id)
            except Exception:
                LOG.exception("已发布法源但未记录变更：%s", source_id)
                return {**result, "impact": {"status": "failed"}}
        return {**result, "impact": self.compute_result(event)}

    def current_task(self, case, activity_id):
        case_id = case["id"]
        if case["status"] == "pending_feishu_approval":
            task = self.enterprise.get_latest_task(case_id, status="succeeded")
        elif case["status"] in APPROVED:
            if activity_id:
                activity = self.transfers.get(activity_id)
                if activity is None or latest_approved_case(activity, self.transfers.reviews(activity_id)) != case_id:
                    return None
            approval = self.governance.get_latest_approval(case_id)
            task = self.enterprise.get_task(approval.task_id) if approval and approval.status in APPROVED else None
        else:
            return None
        return task if task and task.status == "succeeded" and task.case_id == case_id else None

    def compute(self, event, *, today=None):
        today = today or business_today()
        candidates = defaultdict(list)
        for row in self.records.citations(event["source_id"], event["instrument_key"]):
            match = match_reason(event, row, today)
            if match:
                candidates[row["case_id"]].append((match, row))
        flags = []
        for case_id, matches in candidates.items():
            case = self.cases.get_case(case_id)
            if not case:
                continue
            activity_id = self.transfers.for_case(case_id)
            task = self.current_task(case, activity_id)
            if not task:
                continue
            matches = [item for item in matches if item[1]["task_id"] == task.id]
            if not matches:
                continue
            (reason, effective), _ = min(matches, key=lambda item: ORDER.index(item[0][0]))
            flags.append({
                "case_id": case_id, "task_id": task.id, "activity_id": activity_id,
                "reason": reason, "severity": "upcoming" if reason == "superseded_upcoming" else "blocking",
                "chunk_ids": sorted({row["chunk_id"] for _, row in matches}),
                "article_nos": sorted({row["article_no"] for _, row in matches if row["article_no"]}),
                "effective_on": effective,
            })
        return self.records.finish_change(event, flags)

    def compute_result(self, event):
        try:
            return {"status": "computed", "flagged_cases": self.compute(event), "change_event_id": event["id"]}
        except Exception as exc:
            LOG.exception("法规变更影响计算失败：%s", event["id"])
            try:
                self.records.fail_change(event["id"], f"{type(exc).__name__}: {exc}"[:1000])
            except Exception:
                LOG.exception("未能写入法规变更失败状态：%s", event["id"])
            return {"status": "failed", "change_event_id": event["id"]}

    def visible_flags(self, *, status=None, severity=None, owner_id=None):
        flags = self.records.flags(status=status, severity=severity, owner_id=owner_id)
        items = []
        latest = {}
        for row in flags:
            if row["status"] == "open":
                identifier = row["case_id"]
                if identifier not in latest:
                    case = self.cases.get_case(identifier)
                    latest[identifier] = self.current_task(case, self.transfers.for_case(identifier)) if case else None
                task = latest[identifier]
                if not task or task.id != row["task_id"]:
                    continue
            items.append(row)
        return items
