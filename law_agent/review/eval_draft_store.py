"""Persistence and atomic lifecycle transitions for field evaluation drafts."""

from copy import deepcopy
from datetime import datetime
from threading import RLock
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from law_agent.review.case_store import event_id, utc_now

DRAFT_STATES = ("draft", "ready", "exported", "discarded")
APPROVAL_OUTCOMES = ("approved", "conditionally_approved", "rejected")
IMPACT_STATES = ("open", "dismissed", "rereview_started", "resolved")
JSON_FIELDS = {"signals", "original_entities", "intake", "agent_output", "rubric", "reference_basis"}
EDIT_FIELDS = {"question", "material_text", "intake", "reviewer_correction", "rubric",
               "selection_reason", "reference_basis", "deidentified"}


class DuplicateDraft(ValueError):
    def __init__(self, identifier):
        super().__init__("该审查版本已有评测草稿")
        self.identifier = identifier


class DraftValidationError(ValueError):
    def __init__(self, errors):
        super().__init__("草稿未通过校验")
        self.errors = errors


def _changes(row, action, values, validate):
    status = row["status"]
    if action == "edit" and status == "draft" and values.keys() <= EDIT_FIELDS:
        if set(values) - {"deidentified", "reviewer_correction"} and "deidentified" not in values:
            values = {**values, "deidentified": False}
        return values
    if action == "ready" and status == "draft":
        errors = validate(row)
        if errors:
            raise DraftValidationError(errors)
        return {"status": "ready"}
    if action == "reopen" and status == "ready":
        return {"status": "draft", "deidentified": False}
    if action == "discard" and status in {"draft", "ready"}:
        return {"status": "discarded"}
    raise ValueError("当前草稿状态不允许此操作")


def _new_row(*, actor_id, case_id, task_id, signals, question, material_text, intake, agent_output, original_entities):
    return {
        "id": f"evaldraft_{uuid4().hex}", "case_id": case_id, "task_id": task_id, "signals": signals,
        "question": question, "material_text": material_text, "intake": intake, "agent_output": agent_output,
        "original_entities": original_entities, "created_by": actor_id, "updated_by": actor_id,
        "status": "draft", "rubric": {}, "reference_basis": [], "selection_reason": "", "reviewer_correction": "",
        "deidentified": False, "exported_case_id": None, "exported_sha256": None,
    }


def _quality(reviewed_cases, useful, not_useful, correct, wrong, approvals, drafts, impacts):
    return {
        "reviewed_cases": reviewed_cases,
        "conclusion_useful_rate": useful / (useful + not_useful) if useful + not_useful else None,
        "citation_checks": {"correct": correct, "wrong": wrong, "accuracy": correct / (correct + wrong) if correct + wrong else None},
        "approval_outcomes": {key: approvals.get(key, 0) for key in APPROVAL_OUTCOMES},
        "eval_drafts": {key: drafts.get(key, 0) for key in DRAFT_STATES},
        "regulation_impact": {key: impacts.get(key, 0) for key in IMPACT_STATES},
    }


class PostgresEvalDraftStore:
    def __init__(self, dsn):
        self.dsn = dsn

    def _connect(self):
        return psycopg.connect(self.dsn, row_factory=dict_row)

    @staticmethod
    def _audit(conn, row, action):
        conn.execute(
            """INSERT INTO case_events (id, case_id, actor_id, event_type, payload_json)
               VALUES (%s, %s, %s, %s, %s)""",
            (event_id(), row["case_id"], row["updated_by"], f"eval_draft_{action}", Jsonb({
                "draft_id": row["id"], "task_id": row["task_id"], "exported_case_id": row["exported_case_id"],
                "exported_sha256": row["exported_sha256"],
            })),
        )

    def create(self, **values):
        row = _new_row(**values)
        columns = ", ".join(row)
        placeholders = ", ".join(["%s"] * len(row))
        with self._connect() as conn:
            conn.execute("SELECT id FROM review_tasks WHERE id = %s AND case_id = %s FOR UPDATE",
                         (row["task_id"], row["case_id"]))
            previous = conn.execute("SELECT id FROM eval_case_drafts WHERE task_id = %s AND status <> 'discarded'",
                                    (row["task_id"],)).fetchone()
            if previous:
                raise DuplicateDraft(previous["id"])
            created = conn.execute(f"INSERT INTO eval_case_drafts ({columns}) VALUES ({placeholders}) RETURNING *",
                                   [Jsonb(value) if key in JSON_FIELDS else value for key, value in row.items()]).fetchone()
            self._audit(conn, created, "created")
            return created

    def get(self, identifier):
        with self._connect() as conn:
            return conn.execute("SELECT * FROM eval_case_drafts WHERE id = %s", (identifier,)).fetchone()

    def list(self, status=None):
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM eval_case_drafts WHERE (%s::text IS NULL OR status = %s) ORDER BY updated_at DESC, id DESC",
                (status, status),
            ).fetchall()

    def change(self, identifier, action, actor_id, *, values=None, validate=None):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM eval_case_drafts WHERE id = %s FOR UPDATE", (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            patch = _changes(row, action, values or {}, validate)
            values = {**patch, "updated_by": actor_id}
            assignments = ", ".join(f"{key} = %s" for key in values)
            updated = conn.execute(f"UPDATE eval_case_drafts SET {assignments}, updated_at = now() WHERE id = %s RETURNING *",
                                   [*(Jsonb(value) if key in JSON_FIELDS else value for key, value in values.items()), identifier]).fetchone()
            self._audit(conn, updated, action)
            return updated

    def export_ready(self, write_cases):
        with self._connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(7574902604)")
            rows = conn.execute(
                "SELECT * FROM eval_case_drafts WHERE status IN ('ready', 'exported') ORDER BY id FOR UPDATE",
            ).fetchall()
            exported = write_cases(rows)
            for identifier, (case_id, sha256) in exported.items():
                updated = conn.execute(
                    """UPDATE eval_case_drafts SET status = 'exported', exported_case_id = %s,
                       exported_sha256 = %s, updated_at = now() WHERE id = %s RETURNING *""",
                    (case_id, sha256, identifier),
                ).fetchone()
                self._audit(conn, updated, "exported")
            return len(exported)

    def quality(self, since):
        with self._connect() as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            feedback = conn.execute(
                """WITH reviewed AS (
                     SELECT f.* FROM case_feedback f JOIN users u ON f.actor_id = u.id
                     WHERE u.role IN ('reviewer', 'admin') AND f.updated_at >= %s
                   ), verdicts AS (
                     SELECT verdict.value FROM reviewed, LATERAL jsonb_each_text(reviewed.citation_verdicts_json) verdict
                   )
                   SELECT count(DISTINCT case_id) reviewed_cases,
                     count(*) FILTER (WHERE conclusion_useful IS TRUE) useful,
                     count(*) FILTER (WHERE conclusion_useful IS FALSE) not_useful,
                     (SELECT count(*) FROM verdicts WHERE value = 'correct') correct,
                     (SELECT count(*) FROM verdicts WHERE value = 'wrong') wrong
                   FROM reviewed""", (since,),
            ).fetchone()
            approvals = conn.execute(
                """SELECT status, count(*) total FROM
                     (SELECT DISTINCT ON (case_id) * FROM approval_records ORDER BY case_id, created_at DESC, id DESC) latest
                   WHERE COALESCE(decided_at, updated_at) >= %s GROUP BY status""", (since,),
            ).fetchall()
            drafts = conn.execute("SELECT status, count(*) total FROM eval_case_drafts WHERE created_at >= %s GROUP BY status",
                                  (since,)).fetchall()
            impacts = conn.execute("SELECT status, count(*) total FROM case_impact_flags WHERE created_at >= %s GROUP BY status",
                                   (since,)).fetchall()
        return _quality(**feedback, **{key: {row["status"]: row["total"] for row in rows}
                                      for key, rows in (("approvals", approvals), ("drafts", drafts), ("impacts", impacts))})


class InMemoryEvalDraftStore:
    def __init__(self, cases, governance, impact):
        self.cases = cases
        self.governance = governance
        self.impact = impact
        self._rows = {}
        self._lock = RLock()

    def _audit(self, row, action):
        self.cases.add_event(row["case_id"], row["updated_by"], event_type=f"eval_draft_{action}", payload={
            "draft_id": row["id"], "task_id": row["task_id"], "exported_case_id": row["exported_case_id"],
            "exported_sha256": row["exported_sha256"],
        })

    def create(self, **values):
        row = deepcopy(_new_row(**values))
        with self._lock:
            for existing in self._rows.values():
                if existing["task_id"] == row["task_id"] and existing["status"] != "discarded":
                    raise DuplicateDraft(existing["id"])
            now = utc_now()
            row.update(created_at=now, updated_at=now)
            self._audit(row, "created")
            self._rows[row["id"]] = row
            return deepcopy(row)

    def get(self, identifier):
        with self._lock:
            return deepcopy(self._rows.get(identifier))

    def list(self, status=None):
        with self._lock:
            return deepcopy(sorted(
                [row for row in self._rows.values() if status is None or row["status"] == status],
                key=lambda row: (row["updated_at"], row["id"]), reverse=True,
            ))

    def change(self, identifier, action, actor_id, *, values=None, validate=None):
        with self._lock:
            row = self._rows[identifier]
            patch = _changes(row, action, values or {}, validate)
            updated = {**row, **deepcopy(patch), "updated_by": actor_id, "updated_at": utc_now()}
            self._audit(updated, action)
            self._rows[identifier] = updated
            return deepcopy(updated)

    def export_ready(self, write_cases):
        with self._lock:
            exported = write_cases([*self.list("ready"), *self.list("exported")])
            for identifier, (case_id, sha256) in exported.items():
                row = self._rows[identifier]
                row.update(status="exported", exported_case_id=case_id, exported_sha256=sha256, updated_at=utc_now())
                self._audit(row, "exported")
            return len(exported)

    def quality(self, since):
        def within(value):
            return datetime.fromisoformat(value) >= since

        entries = list(self.cases.feedback.values())
        users = {record[0].id: record[0] for record in self.cases.users.values()}
        feedback = [row for row in entries if within(row["updated_at"]) and row["actor_id"] in users
                    and users[row["actor_id"]].role in {"reviewer", "admin"}]
        approvals = {}
        for case_id in {row.case_id for row in list(self.governance.approvals.values())}:
            row = self.governance.get_latest_approval(case_id)
            if row and within(row.decided_at or row.updated_at):
                approvals[row.status] = approvals.get(row.status, 0) + 1
        drafts = self.list()
        impacts = self.impact.flags()
        verdicts = [verdict for row in feedback for verdict in row["citation_verdicts"].values()]
        return _quality(
            len({row["case_id"] for row in feedback}),
            sum(row["conclusion_useful"] is True for row in feedback),
            sum(row["conclusion_useful"] is False for row in feedback),
            verdicts.count("correct"), verdicts.count("wrong"), approvals,
            {key: sum(row["status"] == key and within(row["created_at"]) for row in drafts) for key in DRAFT_STATES},
            {key: sum(row["status"] == key and within(row["created_at"]) for row in impacts) for key in IMPACT_STATES},
        )
