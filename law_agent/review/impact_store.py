"""Storage for regulation changes, citation dependencies and dispositions."""

from copy import deepcopy
from datetime import date
from threading import RLock
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from law_agent.review.case_store import event_id, utc_now
from law_agent.review.transactions import store_connection


def _identifier(prefix):
    return f"{prefix}_{uuid4().hex[:16]}"


def _flag_event(row, event):
    return {
        "flag_id": row["id"], "reason": row["reason"], "source_id": event["source_id"],
        "article_nos": row["article_nos"], "rereview_case_id": row.get("rereview_case_id"),
        "note": row.get("resolution_note"),
    }


class PostgresImpactStore:
    def __init__(self, dsn):
        self.dsn = dsn

    def _connect(self):
        return store_connection(self.dsn, lambda: psycopg.connect(self.dsn, row_factory=dict_row))

    def index_citations(self, task_id, rows):
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM review_tasks WHERE id = %s FOR UPDATE", (task_id,))
            cur.execute("DELETE FROM case_citation_index WHERE task_id = %s AND NOT (chunk_id = ANY(%s))",
                        (task_id, [row["chunk_id"] for row in rows]))
            cur.executemany("""INSERT INTO case_citation_index
                (task_id, case_id, chunk_id, source_id, instrument_key, article_no, title, effective_date, can_cite_clause)
                VALUES (%(task_id)s, %(case_id)s, %(chunk_id)s, %(source_id)s, %(instrument_key)s,
                        %(article_no)s, %(title)s, %(effective_date)s, %(can_cite_clause)s)
                ON CONFLICT (task_id, chunk_id) DO UPDATE SET article_no = EXCLUDED.article_no,
                  title = EXCLUDED.title, effective_date = EXCLUDED.effective_date,
                  can_cite_clause = EXCLUDED.can_cite_clause""", rows)

    def citations(self, source_id, instrument_key):
        with self._connect() as conn:
            return conn.execute(
                """SELECT * FROM case_citation_index WHERE source_id = %s
                   OR (instrument_key = %s AND instrument_key <> '') ORDER BY case_id, chunk_id""",
                (source_id, instrument_key),
            ).fetchall()

    def record_change(self, change, *, job_id=None, created_by=None):
        with self._connect() as conn:
            return conn.execute(
                """INSERT INTO knowledge_change_events
                   (id, job_id, source_id, instrument_key, change_type, detail, created_by)
                   VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (_identifier("kchange"), job_id, change.source_id, change.instrument_key,
                 change.change_type, Jsonb(change.detail), created_by),
            ).fetchone()

    def get_change(self, identifier):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM knowledge_change_events WHERE id = %s", (identifier,)).fetchone()

    def changes(self, limit):
        with self._connect() as conn:
            return conn.execute(
                """SELECT e.*, (SELECT count(*) FROM case_impact_flags f WHERE f.change_event_id = e.id) AS flagged_cases
                   FROM knowledge_change_events e ORDER BY created_at DESC, id DESC LIMIT %s""", (limit,),
            ).fetchall()

    def fail_change(self, identifier, error):
        with self._connect() as conn:
            conn.execute("""UPDATE knowledge_change_events SET impact_status = 'failed', impact_error = %s
                           WHERE id = %s AND impact_status <> 'computed'""",
                         (error, identifier))

    @staticmethod
    def _audit(conn, row, event, actor, kind):
        conn.execute(
            "INSERT INTO case_events (id, case_id, actor_id, event_type, payload_json) VALUES (%s, %s, %s, %s, %s)",
            (event_id(), row["case_id"], actor, kind, Jsonb(_flag_event(row, event))),
        )

    def finish_change(self, event, flags):
        with self._connect() as conn:
            for values in flags:
                row = conn.execute(
                    """INSERT INTO case_impact_flags
                       (id, change_event_id, case_id, task_id, activity_id, reason, severity, chunk_ids, article_nos, effective_on)
                       VALUES (%(id)s, %(change_event_id)s, %(case_id)s, %(task_id)s, %(activity_id)s, %(reason)s,
                               %(severity)s, %(chunk_ids)s, %(article_nos)s, %(effective_on)s)
                       ON CONFLICT (change_event_id, case_id) DO NOTHING RETURNING *""",
                    {**values, "id": _identifier("impact"), "change_event_id": event["id"],
                     "chunk_ids": Jsonb(values["chunk_ids"]), "article_nos": Jsonb(values["article_nos"])},
                ).fetchone()
                if row:
                    self._audit(conn, row, event, event["created_by"], "regulation_impact_flagged")
            conn.execute("UPDATE knowledge_change_events SET impact_status = 'computed', impact_error = NULL WHERE id = %s",
                         (event["id"],))
            return conn.execute("SELECT count(*) AS total FROM case_impact_flags WHERE change_event_id = %s",
                                (event["id"],)).fetchone()["total"]

    def flags(self, *, status=None, severity=None, case_id=None, task_id=None, owner_id=None):
        filters = {
            "f.status": status, "f.severity": severity, "f.case_id": case_id,
            "f.task_id": task_id, "COALESCE(c.owner_id, c.created_by)": owner_id,
        }
        conditions = [f"{key} = %s" for key, value in filters.items() if value is not None]
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self._connect() as conn:
            return conn.execute(
                """SELECT f.*, e.source_id, e.detail->>'title' AS title, c.title AS case_title, a.title AS activity_title
                   FROM case_impact_flags f JOIN knowledge_change_events e ON e.id = f.change_event_id
                   JOIN review_cases c ON c.id = f.case_id LEFT JOIN transfer_activities a ON a.id = f.activity_id"""
                + where + " ORDER BY f.created_at DESC, f.id DESC",
                [value for value in filters.values() if value is not None],
            ).fetchall()

    def open_blocking_count(self, case_id, task_id):
        with self._connect() as conn:
            return conn.execute(
                """SELECT count(*) AS total FROM case_impact_flags
                   WHERE case_id = %s AND task_id = %s AND status = 'open' AND severity = 'blocking'""",
                (case_id, task_id),
            ).fetchone()["total"]

    def get_flag(self, identifier):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM case_impact_flags WHERE id = %s", (identifier,)).fetchone()

    def dispose(self, identifier, *, actor_id, note, start=None):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM case_impact_flags WHERE id = %s FOR UPDATE", (identifier,)).fetchone()
            if row is None or row["status"] != "open":
                raise ValueError("影响提示已处理，请刷新")
            review_id = start(row) if start else None
            status = "rereview_started" if review_id else "dismissed"
            row = conn.execute(
                """UPDATE case_impact_flags SET status = %s, resolution_note = %s, resolved_by = %s,
                   resolved_at = CASE WHEN %s::text IS NULL THEN now() ELSE NULL END, rereview_case_id = %s
                   WHERE id = %s RETURNING *""", (status, note, actor_id, review_id, review_id, identifier),
            ).fetchone()
            event = conn.execute("SELECT * FROM knowledge_change_events WHERE id = %s",
                                 (row["change_event_id"],)).fetchone()
            self._audit(conn, row, event, actor_id, f"regulation_impact_{status}")
            return row

    def resolve(self, rereview_case_id):
        with self._connect() as conn:
            rows = conn.execute(
                """UPDATE case_impact_flags SET status = 'resolved', resolved_at = now()
                   WHERE rereview_case_id = %s AND status = 'rereview_started' RETURNING *""", (rereview_case_id,),
            ).fetchall()
            for row in rows:
                event = conn.execute("SELECT * FROM knowledge_change_events WHERE id = %s",
                                     (row["change_event_id"],)).fetchone()
                self._audit(conn, row, event, row["resolved_by"], "regulation_impact_resolved")

    def promote(self, today):
        with self._connect() as conn:
            rows = conn.execute(
                """UPDATE case_impact_flags SET severity = 'blocking', reason = 'superseded'
                   WHERE status = 'open' AND reason = 'superseded_upcoming' AND effective_on <= %s RETURNING *""", (today,),
            ).fetchall()
            for row in rows:
                event = conn.execute("SELECT * FROM knowledge_change_events WHERE id = %s",
                                     (row["change_event_id"],)).fetchone()
                self._audit(conn, row, event, event["created_by"], "regulation_impact_promoted")
            return len(rows)


class InMemoryImpactStore:
    def __init__(self, cases, transfers):
        self.cases = cases
        self.transfers = transfers
        self._lock = RLock()
        self._citations = {}
        self._changes = {}
        self._flags = {}

    def index_citations(self, task_id, rows):
        with self._lock:
            current = {row["chunk_id"] for row in rows}
            self._citations = {key: row for key, row in self._citations.items()
                               if key[0] != task_id or key[1] in current}
            for row in rows:
                key = (row["task_id"], row["chunk_id"])
                indexed = self._citations.get(key)
                self._citations[key] = {**deepcopy(row), "instrument_key": indexed["instrument_key"] if indexed else row["instrument_key"]}

    def citations(self, source_id, instrument_key):
        with self._lock:
            return deepcopy([row for row in self._citations.values() if row["source_id"] == source_id
                             or (instrument_key and row["instrument_key"] == instrument_key)])

    def record_change(self, change, *, job_id=None, created_by=None):
        with self._lock:
            identifier = _identifier("kchange")
            self._changes[identifier] = {
                **deepcopy(vars(change)), "id": identifier, "job_id": job_id, "created_by": created_by,
                "created_at": utc_now(), "impact_status": "pending", "impact_error": None,
            }
            return deepcopy(self._changes[identifier])

    def get_change(self, identifier):
        with self._lock:
            return deepcopy(self._changes.get(identifier))

    def changes(self, limit):
        with self._lock:
            return deepcopy([{**row, "flagged_cases": sum(flag["change_event_id"] == row["id"] for flag in self._flags.values())}
                             for row in list(self._changes.values())[::-1][:limit]])

    def fail_change(self, identifier, error):
        with self._lock:
            if self._changes[identifier]["impact_status"] != "computed":
                self._changes[identifier].update(impact_status="failed", impact_error=error)

    def _audit(self, row, actor, kind):
        self.cases.add_event(row["case_id"], actor, event_type=kind,
                             payload=_flag_event(row, self._changes[row["change_event_id"]]))

    def finish_change(self, event, flags):
        with self._lock:
            existing = {row["case_id"] for row in self._flags.values() if row["change_event_id"] == event["id"]}
            for values in flags:
                if values["case_id"] in existing:
                    continue
                identifier = _identifier("impact")
                row = {**deepcopy(values), "id": identifier, "change_event_id": event["id"],
                       "created_at": utc_now(), "status": "open", "resolution_note": None,
                       "resolved_by": None, "resolved_at": None, "rereview_case_id": None}
                self._audit(row, event["created_by"], "regulation_impact_flagged")
                self._flags[identifier] = row
                existing.add(values["case_id"])
            self._changes[event["id"]].update(impact_status="computed", impact_error=None)
            return len(existing)

    def flags(self, *, status=None, severity=None, case_id=None, task_id=None, owner_id=None):
        with self._lock:
            items = []
            for row in reversed(list(self._flags.values())):
                if any(value is not None and row[key] != value for key, value in
                       (("status", status), ("severity", severity), ("case_id", case_id), ("task_id", task_id))):
                    continue
                case = self.cases.get_case(row["case_id"])
                if case is None or (owner_id is not None and owner_id != (case.get("owner_id") or case["created_by"])):
                    continue
                event = self._changes[row["change_event_id"]]
                activity = self.transfers.get(row["activity_id"]) if row["activity_id"] else None
                items.append({**row, "source_id": event["source_id"], "title": event["detail"]["title"],
                              "case_title": case["title"], "activity_title": activity["title"] if activity else None})
            return deepcopy(items)

    def open_blocking_count(self, case_id, task_id):
        return len(self.flags(case_id=case_id, task_id=task_id, status="open", severity="blocking"))

    def get_flag(self, identifier):
        with self._lock:
            return deepcopy(self._flags.get(identifier))

    def dispose(self, identifier, *, actor_id, note, start=None):
        with self._lock:
            row = self._flags.get(identifier)
            if row is None or row["status"] != "open":
                raise ValueError("影响提示已处理，请刷新")
            review_id = start(row) if start else None
            status = "rereview_started" if review_id else "dismissed"
            row.update(status=status, resolution_note=note, resolved_by=actor_id,
                       rereview_case_id=review_id, resolved_at=None if review_id else utc_now())
            self._audit(row, actor_id, f"regulation_impact_{status}")
            return deepcopy(row)

    def resolve(self, rereview_case_id):
        with self._lock:
            for row in self._flags.values():
                if row["rereview_case_id"] == rereview_case_id and row["status"] == "rereview_started":
                    row.update(status="resolved", resolved_at=utc_now())
                    self._audit(row, row["resolved_by"], "regulation_impact_resolved")

    def promote(self, today: date):
        changed = 0
        with self._lock:
            for row in self._flags.values():
                if row["status"] == "open" and row["reason"] == "superseded_upcoming" and row["effective_on"] <= today:
                    row.update(reason="superseded", severity="blocking")
                    self._audit(row, self._changes[row["change_event_id"]]["created_by"], "regulation_impact_promoted")
                    changed += 1
        return changed
