"""Persistence for ongoing transfer activities and their linked case history."""

from __future__ import annotations

from datetime import date
from threading import Lock
from typing import Any
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from law_agent.review.case_store import (
    InMemoryCaseStore,
    PostgresCaseStore,
    UserRecord,
    event_id,
    utc_now,
)

APPROVED = ("approved", "conditionally_approved")
CLOSED = (*APPROVED, "rejected")


def _activity_id() -> str:
    return f"transfer_{uuid4().hex[:16]}"


class PostgresTransferStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def list(self, user: UserRecord) -> list[dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            if user.role == "requester":
                cur.execute("SELECT * FROM transfer_activities WHERE owner_id = %s ORDER BY next_review_on, title", (user.id,))
            else:
                cur.execute("SELECT * FROM transfer_activities ORDER BY next_review_on, title")
            return cur.fetchall()

    def get(self, activity_id: str) -> dict[str, Any] | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM transfer_activities WHERE id = %s", (activity_id,))
            return cur.fetchone()

    def for_case(self, case_id: str) -> str | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT id FROM transfer_activities WHERE origin_case_id = %s
                   UNION ALL SELECT activity_id FROM transfer_activity_reviews WHERE case_id = %s""",
                (case_id, case_id),
            )
            row = cur.fetchone()
            return row["id"] if row else None

    @staticmethod
    def _reviews(cur: Any, activity_id: str) -> list[dict[str, Any]]:
        cur.execute(
            """SELECT r.*, c.title, c.status FROM transfer_activity_reviews r
               JOIN review_cases c ON c.id = r.case_id
               WHERE r.activity_id = %s ORDER BY r.created_at DESC, r.case_id DESC""",
            (activity_id,),
        )
        return cur.fetchall()

    def reviews(self, activity_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            return self._reviews(cur, activity_id)

    def register(self, **values: Any) -> str:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO transfer_activities (id, origin_case_id, owner_id, title, business_entity, next_review_on)
                   VALUES (%(id)s, %(origin_case_id)s, %(owner_id)s, %(title)s, %(business_entity)s, %(next_review_on)s)
                   ON CONFLICT (origin_case_id) DO NOTHING RETURNING id""",
                {"id": _activity_id(), **values},
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError("这个案件已经登记过")
            return row["id"]

    def reschedule(self, activity_id: str, next_review_on: date) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE transfer_activities SET next_review_on = %s WHERE id = %s", (next_review_on, activity_id))

    def start_review(
        self, activity: dict[str, Any], baseline_id: str, *, business_entity: str,
        note: str, actor_id: str, case_values: dict[str, Any],
    ) -> str:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM transfer_activities WHERE id = %s FOR UPDATE", (activity["id"],))
            _check_review_start(activity, self._reviews(cur, activity["id"]), baseline_id)
            case = PostgresCaseStore.insert_case(cur, **case_values)
            cur.execute(
                """INSERT INTO transfer_activity_reviews (case_id, activity_id, baseline_case_id, business_entity, change_note)
                   VALUES (%s, %s, %s, %s, %s)""",
                (case["id"], activity["id"], baseline_id, business_entity, note),
            )
            self._record(cur, case["id"], actor_id, "transfer_review_created", {"activity_id": activity["id"]})
            return case["id"]

    def review_for_case(self, case_id: str) -> dict[str, Any] | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM transfer_activity_reviews WHERE case_id = %s", (case_id,))
            return cur.fetchone()

    def record_impact(self, case_id: str, impact: dict[str, Any]) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE transfer_activity_reviews SET impact = %s
                   WHERE case_id = %s AND impact->>'task_id' IS DISTINCT FROM %s RETURNING case_id""",
                (Jsonb(impact), case_id, impact["task_id"]),
            )
            if cur.fetchone() is None:
                raise ValueError("此版本已有法务影响确认")
            self._record(cur, case_id, impact["reviewer_id"], "transfer_impact_confirmed", impact)

    @staticmethod
    def _record(cur: Any, case_id: str, actor_id: str, event: str, payload: dict[str, Any]) -> None:
        cur.execute(
            """INSERT INTO case_events (id, case_id, actor_id, event_type, payload_json)
               VALUES (%s, %s, %s, %s, %s)""",
            (event_id(), case_id, actor_id, event, Jsonb(payload)),
        )


class InMemoryTransferStore:
    def __init__(self, cases: InMemoryCaseStore) -> None:
        self.cases = cases
        self.activities: dict[str, dict[str, Any]] = {}
        self.links: dict[str, dict[str, Any]] = {}
        self.lock = Lock()

    def list(self, user: UserRecord) -> list[dict[str, Any]]:
        rows = [dict(row) for row in self.activities.values() if user.role != "requester" or row["owner_id"] == user.id]
        return sorted(rows, key=lambda row: (row["next_review_on"], row["title"]))

    def get(self, activity_id: str) -> dict[str, Any] | None:
        row = self.activities.get(activity_id)
        return dict(row) if row else None

    def for_case(self, case_id: str) -> str | None:
        link = self.links.get(case_id)
        if link:
            return link["activity_id"]
        return next((key for key, row in self.activities.items() if row["origin_case_id"] == case_id), None)

    def reviews(self, activity_id: str) -> list[dict[str, Any]]:
        rows = []
        for row in reversed(list(self.links.values())):
            if row["activity_id"] == activity_id:
                case = self.cases.get_case(row["case_id"])
                rows.append({**row, "title": case["title"], "status": case["status"]})
        return rows

    def register(self, **values: Any) -> str:
        with self.lock:
            if self.for_case(values["origin_case_id"]):
                raise ValueError("这个案件已经登记过")
            identifier = _activity_id()
            self.activities[identifier] = {"id": identifier, "created_at": utc_now(), **values}
            return identifier

    def reschedule(self, activity_id: str, next_review_on: date) -> None:
        with self.lock:
            self.activities[activity_id]["next_review_on"] = next_review_on

    def start_review(
        self, activity: dict[str, Any], baseline_id: str, *, business_entity: str,
        note: str, actor_id: str, case_values: dict[str, Any],
    ) -> str:
        with self.lock:
            _check_review_start(activity, self.reviews(activity["id"]), baseline_id)
            case = self.cases.create_case(**case_values)
            self.links[case["id"]] = {
                "case_id": case["id"], "activity_id": activity["id"], "baseline_case_id": baseline_id,
                "business_entity": business_entity, "change_note": note, "impact": None, "created_at": utc_now(),
            }
            self.cases.add_event(case["id"], actor_id, event_type="transfer_review_created", payload={"activity_id": activity["id"]})
            return case["id"]

    def review_for_case(self, case_id: str) -> dict[str, Any] | None:
        row = self.links.get(case_id)
        return dict(row) if row else None

    def record_impact(self, case_id: str, impact: dict[str, Any]) -> None:
        with self.lock:
            row = self.links[case_id]
            if (row["impact"] or {}).get("task_id") == impact["task_id"]:
                raise ValueError("此版本已有法务影响确认")
            row["impact"] = impact
            self.cases.add_event(case_id, impact["reviewer_id"], event_type="transfer_impact_confirmed", payload=impact)


def latest_approved_case(activity: dict[str, Any], reviews: list[dict[str, Any]]) -> str:
    return next((row["case_id"] for row in reviews if row["status"] in APPROVED), activity["origin_case_id"])


def _check_review_start(activity: dict[str, Any], reviews: list[dict[str, Any]], baseline_id: str) -> None:
    if any(row["status"] not in CLOSED for row in reviews):
        raise ValueError("已有进行中的复审，请继续处理该案件")
    if latest_approved_case(activity, reviews) != baseline_id:
        raise ValueError("最近一次审批已更新，请刷新后发起复审")
