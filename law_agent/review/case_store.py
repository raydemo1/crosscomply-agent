"""Persistence and authentication primitives for the CrossComply workbench.

The production application uses PostgreSQL for all case state.  The in-memory
store is intentionally an explicit test double injected by API tests; it is
never selected implicitly at runtime.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from pwdlib import PasswordHash

from law_agent.review.workflow import CASE_TRANSITIONS, CaseStatus

UserRole = Literal["requester", "reviewer", "admin"]
RemediationPlanStatus = Literal["draft", "active", "completed", "cancelled"]
RemediationTaskStatus = Literal["open", "in_progress", "pending_review", "completed"]
RemediationPriority = Literal["high", "medium", "low"]


def _jsonb(value: Any) -> Jsonb:
    return Jsonb(value or {})


@dataclass(frozen=True)
class UserRecord:
    id: str
    username: str
    display_name: str
    role: UserRole

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
        }


@dataclass
class MemoryCase:
    id: str
    case_number: str
    title: str
    question: str
    material_text: str
    material_source: str | None
    intake: dict[str, Any]
    status: CaseStatus
    review_mode: str
    rerank_mode: str
    created_by: str
    owner_id: str | None = None
    facts_confirmed: bool = False
    risk_level: str | None = None
    trace_id: str | None = None
    response: dict[str, Any] | None = None
    created_at: str = field(default_factory=lambda: utc_now())
    updated_at: str = field(default_factory=lambda: utc_now())


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def case_id() -> str:
    return f"case_{uuid4().hex[:16]}"


def case_number() -> str:
    return f"CC-{datetime.now(UTC):%Y%m%d}-{uuid4().hex[:8].upper()}"


def event_id() -> str:
    return f"event_{uuid4().hex[:16]}"


def remediation_plan_id() -> str:
    return f"remediation_plan_{uuid4().hex[:16]}"


def remediation_task_id() -> str:
    return f"remediation_task_{uuid4().hex[:16]}"


def remediation_submission_id() -> str:
    return f"remediation_submission_{uuid4().hex[:16]}"


def remediation_evidence_id() -> str:
    return f"remediation_evidence_{uuid4().hex[:16]}"


def remediation_event_id() -> str:
    return f"remediation_event_{uuid4().hex[:16]}"


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _row_case(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "case_number": row.get("case_number", row["id"]),
        "title": row["title"],
        "question": row["question"],
        "material_text": row["material_text"],
        "material_source": row["material_source"],
        "intake": row.get("intake_json") or {},
        "status": row["status"],
        "review_mode": row["review_mode"],
        "rerank_mode": row["rerank_mode"],
        "created_by": row["created_by"],
        "owner_id": row["owner_id"],
        "facts_confirmed": row["facts_confirmed"],
        "risk_level": row["risk_level"],
        "trace_id": row["trace_id"],
        "response": row.get("response_json"),
        "created_at": _json_value(row["created_at"]),
        "updated_at": _json_value(row["updated_at"]),
    }


def _row_user(row: dict[str, Any]) -> UserRecord:
    return UserRecord(
        id=row["id"],
        username=row["username"],
        display_name=row["display_name"],
        role=row["role"],
    )


class CaseStore(Protocol):
    def initialize(self) -> None: ...

    def authenticate(self, username: str, password: str) -> UserRecord | None: ...

    def create_session(self, user_id: str, ttl_hours: int) -> tuple[str, str]: ...

    def get_user_by_session(self, token: str) -> UserRecord | None: ...

    def delete_session(self, token: str) -> None: ...

    def create_case(self, **kwargs: Any) -> dict[str, Any]: ...

    def get_case(self, case_id: str) -> dict[str, Any] | None: ...

    def list_cases(self, user: UserRecord, query: str | None = None) -> list[dict[str, Any]]: ...

    def update_case(self, case_id: str, **kwargs: Any) -> dict[str, Any]: ...

    def add_event(self, case_id: str, actor_id: str, **kwargs: Any) -> dict[str, Any]: ...

    def list_events(self, case_id: str) -> list[dict[str, Any]]: ...

    # Internal report projection; this does not read or write the removed case_actions table.
    def list_actions(self, case_id: str) -> list[dict[str, Any]]: ...

    def get_user(self, user_id: str) -> UserRecord | None: ...

    def list_assignable_users(self) -> list[UserRecord]: ...

    def get_remediation_plan(self, case_id: str) -> dict[str, Any] | None: ...

    def create_remediation_plan(self, case_id: str, created_by: str, **kwargs: Any) -> dict[str, Any]: ...

    def activate_remediation_plan(self, plan_id: str, **kwargs: Any) -> dict[str, Any]: ...

    def cancel_remediation_plan(self, plan_id: str, **kwargs: Any) -> dict[str, Any]: ...

    def list_remediation_tasks(self, case_id: str | None = None, *, assignee_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]: ...

    def get_remediation_task(self, task_id: str) -> dict[str, Any] | None: ...

    def create_remediation_task(self, plan_id: str, **kwargs: Any) -> dict[str, Any]: ...

    def update_remediation_task(self, task_id: str, **kwargs: Any) -> dict[str, Any]: ...

    def start_remediation_task(self, task_id: str) -> dict[str, Any]: ...

    def create_remediation_submission(self, task_id: str, **kwargs: Any) -> dict[str, Any]: ...

    def get_remediation_submission(self, submission_id: str) -> dict[str, Any] | None: ...

    def review_remediation_submission(self, submission_id: str, **kwargs: Any) -> dict[str, Any]: ...

    def list_remediation_events(self, plan_id: str) -> list[dict[str, Any]]: ...

    def add_remediation_event(self, plan_id: str, actor_id: str, **kwargs: Any) -> dict[str, Any]: ...

    def get_feedback(self, case_id: str) -> dict[str, Any] | None: ...

    def save_feedback(self, case_id: str, actor_id: str, **kwargs: Any) -> dict[str, Any]: ...

    def dashboard_summary(self, user: UserRecord) -> dict[str, Any]: ...


class PostgresCaseStore:
    """PostgreSQL-backed case store used by the running service."""

    _password_hasher = PasswordHash.recommended()

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def initialize(self) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT version_num FROM alembic_version WHERE version_num = %s",
                ("0003_case_templates",),
            )
            if cur.fetchone() is None:
                raise RuntimeError("数据库未应用 CrossComply 最新结构，请先运行 alembic upgrade head")

    def authenticate(self, username: str, password: str) -> UserRecord | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, display_name, role, password_hash
                FROM users WHERE username = %s AND active = TRUE
                """,
                (username.strip().lower(),),
            )
            row = cur.fetchone()
        if row is None or not self._password_hasher.verify(password, row["password_hash"]):
            return None
        return _row_user(row)

    def create_session(self, user_id: str, ttl_hours: int) -> tuple[str, str]:
        token = secrets.token_urlsafe(32)
        expires = datetime.now(UTC) + timedelta(hours=ttl_hours)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sessions (id, user_id, token_hash, expires_at) VALUES (%s, %s, %s, %s)",
                (f"session_{uuid4().hex[:16]}", user_id, hash_session_token(token), expires),
            )
            conn.commit()
        return token, expires.isoformat()

    def get_user_by_session(self, token: str) -> UserRecord | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id, u.username, u.display_name, u.role
                FROM sessions s JOIN users u ON u.id = s.user_id
                WHERE s.token_hash = %s AND s.expires_at > now() AND u.active = TRUE
                """,
                (hash_session_token(token),),
            )
            row = cur.fetchone()
        return _row_user(row) if row else None

    def get_user(self, user_id: str) -> UserRecord | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, display_name, role FROM users WHERE id = %s AND active = TRUE",
                (user_id,),
            )
            row = cur.fetchone()
        return _row_user(row) if row else None

    def list_assignable_users(self) -> list[UserRecord]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, display_name, role FROM users
                WHERE active = TRUE AND role IN ('requester', 'reviewer', 'admin')
                ORDER BY display_name, username
                """
            )
            return [_row_user(row) for row in cur.fetchall()]

    def delete_session(self, token: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("DELETE FROM sessions WHERE token_hash = %s", (hash_session_token(token),))
            conn.commit()

    def create_case(self, **kwargs: Any) -> dict[str, Any]:
        identifier = kwargs.get("id") or case_id()
        now = datetime.now(UTC)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO review_cases (
                    id, case_number, title, question, material_text, material_source, intake_json,
                    status, review_mode, rerank_mode, created_by, owner_id,
                    facts_confirmed, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    identifier,
                    kwargs.get("case_number") or case_number(),
                    kwargs.get("title") or kwargs["question"][:80],
                    kwargs["question"],
                    kwargs["material_text"],
                    kwargs.get("material_source"),
                    _jsonb(kwargs.get("intake")),
                    kwargs.get("status", "draft"),
                    kwargs.get("review_mode", "llm"),
                    kwargs.get("rerank_mode", "off"),
                    kwargs["created_by"],
                    kwargs.get("owner_id"),
                    kwargs.get("facts_confirmed", False),
                    now,
                    now,
                ),
            )
            row = cur.fetchone()
            conn.commit()
        return _row_case(row)

    def get_case(self, identifier: str) -> dict[str, Any] | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM review_cases WHERE id = %s", (identifier,))
            row = cur.fetchone()
        return _row_case(row) if row else None

    def list_cases(self, user: UserRecord, query: str | None = None) -> list[dict[str, Any]]:
        conditions = []
        params: list[Any] = []
        if user.role == "requester":
            conditions.append("created_by = %s")
            params.append(user.id)
        if query and query.strip():
            conditions.append("(question ILIKE %s OR id ILIKE %s OR title ILIKE %s)")
            pattern = f"%{query.strip()}%"
            params.extend([pattern, pattern, pattern])
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT * FROM review_cases {where} ORDER BY updated_at DESC", params)
            return [_row_case(row) for row in cur.fetchall()]

    def update_case(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        allowed = {
            "title",
            "question",
            "material_text",
            "material_source",
            "intake_json",
            "status",
            "review_mode",
            "rerank_mode",
            "owner_id",
            "facts_confirmed",
            "risk_level",
            "trace_id",
            "response_json",
        }
        updates = {key: value for key, value in kwargs.items() if key in allowed}
        for key in ("intake_json", "response_json"):
            if key in updates:
                updates[key] = _jsonb(updates[key])
        if not updates:
            case = self.get_case(identifier)
            if case is None:
                raise KeyError(identifier)
            return case
        updates["updated_at"] = datetime.now(UTC)
        assignments = ", ".join(f"{key} = %s" for key in updates)
        values = list(updates.values()) + [identifier]
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE review_cases SET {assignments} WHERE id = %s RETURNING *",
                values,
            )
            row = cur.fetchone()
            conn.commit()
        if row is None:
            raise KeyError(identifier)
        return _row_case(row)

    def add_event(self, identifier: str, actor_id: str, **kwargs: Any) -> dict[str, Any]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO case_events (id, case_id, actor_id, event_type, from_status, to_status, payload_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    event_id(),
                    identifier,
                    actor_id,
                    kwargs["event_type"],
                    kwargs.get("from_status"),
                    kwargs.get("to_status"),
                    _jsonb(kwargs.get("payload")),
                ),
            )
            row = cur.fetchone()
            conn.commit()
        return {
            "id": row["id"],
            "case_id": row["case_id"],
            "actor_id": row["actor_id"],
            "event_type": row["event_type"],
            "from_status": row["from_status"],
            "to_status": row["to_status"],
            "payload": row["payload_json"] or {},
            "created_at": _json_value(row["created_at"]),
        }

    def list_events(self, identifier: str) -> list[dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM case_events WHERE case_id = %s ORDER BY created_at", (identifier,)
            )
            return [
                {
                    "id": row["id"],
                    "case_id": row["case_id"],
                    "actor_id": row["actor_id"],
                    "event_type": row["event_type"],
                    "from_status": row["from_status"],
                    "to_status": row["to_status"],
                    "payload": row["payload_json"] or {},
                    "created_at": _json_value(row["created_at"]),
                }
                for row in cur.fetchall()
            ]

    @staticmethod
    def _remediation_task_dict(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"], "plan_id": row["plan_id"], "case_id": row["case_id"],
            "title": row["title"], "description": row["description"],
            "acceptance_criteria": row["acceptance_criteria"],
            "source_recommendation_index": row["source_recommendation_index"],
            "source_recommendation": row["source_recommendation"],
            "assignee_id": row["assignee_id"], "priority": row["priority"],
            "due_date": _json_value(row["due_date"]), "status": row["status"],
            "version": row["version"], "created_at": _json_value(row["created_at"]),
            "updated_at": _json_value(row["updated_at"]),
        }

    @staticmethod
    def _remediation_plan_dict(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"], "case_id": row["case_id"], "created_by": row["created_by"],
            "status": row["status"], "no_remediation_reason": row["no_remediation_reason"],
            "version": row["version"], "created_at": _json_value(row["created_at"]),
            "updated_at": _json_value(row["updated_at"]),
        }

    @staticmethod
    def _remediation_submission_dict(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"], "task_id": row["task_id"], "submitted_by": row["submitted_by"],
            "note": row["note"], "status": row["status"], "reviewed_by": row["reviewed_by"],
            "review_note": row["review_note"], "reviewed_at": _json_value(row["reviewed_at"]),
            "created_at": _json_value(row["created_at"]),
        }

    @staticmethod
    def _remediation_evidence_dict(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"], "submission_id": row["submission_id"], "kind": row["kind"],
            "label": row["label"], "uri": row["uri"], "object_key": row["object_key"],
            "content_type": row["content_type"], "sha256": row["sha256"],
            "byte_size": row["byte_size"], "created_at": _json_value(row["created_at"]),
        }

    def get_remediation_plan(self, identifier: str) -> dict[str, Any] | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM remediation_plans WHERE case_id = %s", (identifier,))
            row = cur.fetchone()
            if row is None:
                return None
            plan = self._remediation_plan_dict(row)
            cur.execute("SELECT * FROM remediation_tasks WHERE plan_id = %s ORDER BY created_at", (row["id"],))
            tasks = [self._remediation_task_dict(item) for item in cur.fetchall()]
            for task in tasks:
                cur.execute("SELECT * FROM remediation_submissions WHERE task_id = %s ORDER BY created_at", (task["id"],))
                task["submissions"] = [self._remediation_submission_dict(item) for item in cur.fetchall()]
                for submission in task["submissions"]:
                    cur.execute("SELECT * FROM remediation_evidence WHERE submission_id = %s ORDER BY created_at", (submission["id"],))
                    submission["evidence"] = [self._remediation_evidence_dict(item) for item in cur.fetchall()]
            plan["tasks"] = tasks
            cur.execute("SELECT * FROM remediation_events WHERE plan_id = %s ORDER BY created_at", (row["id"],))
            plan["events"] = [
                {"id": item["id"], "plan_id": item["plan_id"], "task_id": item["task_id"],
                 "actor_id": item["actor_id"], "event_type": item["event_type"],
                 "payload": item["payload_json"] or {}, "created_at": _json_value(item["created_at"])}
                for item in cur.fetchall()
            ]
            return plan

    def list_actions(self, identifier: str) -> list[dict[str, Any]]:
        """Return remediation tasks in the legacy report detail shape."""
        return [self._report_task_shape(item) for item in self.list_remediation_tasks(identifier)]

    @staticmethod
    def _report_task_shape(item: dict[str, Any]) -> dict[str, Any]:
        report_item = dict(item)
        report_item["owner_role"] = item.get("assignee_id") or "未分派"
        report_item["evidence_expected"] = item.get("acceptance_criteria") or ""
        report_item["status"] = {
            "open": "open", "in_progress": "in_progress", "pending_review": "in_progress", "completed": "completed"
        }.get(str(item.get("status")), "open")
        return report_item

    def create_remediation_plan(self, identifier: str, created_by: str, **kwargs: Any) -> dict[str, Any]:
        plan_identifier = kwargs.get("id") or remediation_plan_id()
        tasks = kwargs.get("tasks") or []
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO remediation_plans (id, case_id, created_by, no_remediation_reason)
                VALUES (%s, %s, %s, %s) RETURNING *""",
                (plan_identifier, identifier, created_by, kwargs.get("no_remediation_reason")),
            )
            plan_row = cur.fetchone()
            if plan_row is None:
                raise KeyError(identifier)
            for task in tasks:
                cur.execute(
                    """INSERT INTO remediation_tasks (
                    id, plan_id, case_id, title, description, acceptance_criteria,
                    source_recommendation_index, source_recommendation, assignee_id, priority, due_date
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (task.get("id") or remediation_task_id(), plan_identifier, identifier, task["title"],
                     task.get("description", ""), task.get("acceptance_criteria", ""),
                     task.get("source_recommendation_index"), task.get("source_recommendation"),
                     task.get("assignee_id"), task.get("priority", "medium"), task.get("due_date")),
                )
            conn.commit()
        return self.get_remediation_plan(identifier) or {"id": plan_identifier, "case_id": identifier}

    def activate_remediation_plan(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        expected = kwargs.get("expected_version")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM remediation_plans WHERE id = %s FOR UPDATE", (identifier,))
            plan = cur.fetchone()
            if plan is None:
                raise KeyError(identifier)
            if expected is not None and plan["version"] != expected:
                raise ValueError("整改计划版本已变化，请刷新后重试")
            cur.execute("SELECT COUNT(*) AS count FROM remediation_tasks WHERE plan_id = %s", (identifier,))
            count = int(cur.fetchone()["count"])
            if not count and not (plan["no_remediation_reason"] or "").strip():
                raise ValueError("整改计划至少需要一项任务，或填写无需整改的理由")
            cur.execute("SELECT COUNT(*) AS count FROM remediation_tasks WHERE plan_id = %s AND (assignee_id IS NULL OR due_date IS NULL)", (identifier,))
            incomplete = int(cur.fetchone()["count"])
            if incomplete:
                raise ValueError("整改任务必须指定负责人和截止日期后才能激活")
            target_status = "completed" if count == 0 else "active"
            cur.execute("UPDATE remediation_plans SET status = %s, version = version + 1, updated_at = now() WHERE id = %s RETURNING *", (target_status, identifier))
            row = cur.fetchone()
            conn.commit()
        return self.get_remediation_plan(row["case_id"]) or self._remediation_plan_dict(row)

    def cancel_remediation_plan(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE remediation_plans SET status = 'cancelled', version = version + 1, updated_at = now() WHERE id = %s RETURNING *", (identifier,))
            row = cur.fetchone()
            conn.commit()
        if row is None:
            raise KeyError(identifier)
        return self.get_remediation_plan(row["case_id"]) or self._remediation_plan_dict(row)

    def list_remediation_tasks(self, case_id: str | None = None, *, assignee_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        conditions: list[str] = []
        params: list[Any] = []
        if case_id:
            conditions.append("case_id = %s")
            params.append(case_id)
        if assignee_id:
            conditions.append("assignee_id = %s")
            params.append(assignee_id)
        if status:
            conditions.append("status = %s")
            params.append(status)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT * FROM remediation_tasks {where} ORDER BY due_date NULLS LAST, created_at", params)
            return [self._remediation_task_dict(row) for row in cur.fetchall()]

    def get_remediation_task(self, identifier: str) -> dict[str, Any] | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM remediation_tasks WHERE id = %s", (identifier,))
            row = cur.fetchone()
        if row is None:
            return None
        task = self._remediation_task_dict(row)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM remediation_submissions WHERE task_id = %s ORDER BY created_at", (identifier,))
            task["submissions"] = [self._remediation_submission_dict(item) for item in cur.fetchall()]
            for submission in task["submissions"]:
                cur.execute("SELECT * FROM remediation_evidence WHERE submission_id = %s ORDER BY created_at", (submission["id"],))
                submission["evidence"] = [self._remediation_evidence_dict(item) for item in cur.fetchall()]
        return task

    def create_remediation_task(self, plan_id: str, **kwargs: Any) -> dict[str, Any]:
        plan = self._plan_case_id(plan_id)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("""INSERT INTO remediation_tasks (id, plan_id, case_id, title, description, acceptance_criteria, source_recommendation_index, source_recommendation, assignee_id, priority, due_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING *""",
                (remediation_task_id(), plan, kwargs["case_id"], kwargs["title"], kwargs.get("description", ""), kwargs.get("acceptance_criteria", ""), kwargs.get("source_recommendation_index"), kwargs.get("source_recommendation"), kwargs.get("assignee_id"), kwargs.get("priority", "medium"), kwargs.get("due_date")))
            row = cur.fetchone()
            conn.commit()
        return self._remediation_task_dict(row)

    def _plan_case_id(self, plan_id: str) -> str:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT case_id FROM remediation_plans WHERE id = %s", (plan_id,))
            row = cur.fetchone()
        if row is None:
            raise KeyError(plan_id)
        return row["case_id"]

    def update_remediation_task(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        allowed = {"title", "description", "acceptance_criteria", "assignee_id", "priority", "due_date"}
        updates = {key: value for key, value in kwargs.items() if key in allowed}
        if not updates:
            task = self.get_remediation_task(identifier)
            if task is None:
                raise KeyError(identifier)
            return task
        expected = kwargs.get("expected_version")
        assignments = ", ".join(f"{key} = %s" for key in updates)
        values = list(updates.values()) + [identifier]
        version_clause = " AND version = %s" if expected is not None else ""
        if expected is not None:
            values.append(expected)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"UPDATE remediation_tasks SET {assignments}, version = version + 1, updated_at = now() WHERE id = %s{version_clause} RETURNING *", values)
            row = cur.fetchone()
            conn.commit()
        if row is None:
            raise ValueError("整改任务不存在或版本已变化")
        return self._remediation_task_dict(row)

    def start_remediation_task(self, identifier: str) -> dict[str, Any]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE remediation_tasks SET status = 'in_progress', version = version + 1, updated_at = now() WHERE id = %s AND status = 'open' RETURNING *", (identifier,))
            row = cur.fetchone()
            conn.commit()
        if row is None:
            raise ValueError("整改任务当前不能开始")
        return self._remediation_task_dict(row)

    def create_remediation_submission(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        submission_identifier = remediation_submission_id()
        evidence = kwargs.get("evidence") or []
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM remediation_tasks WHERE id = %s FOR UPDATE", (identifier,))
            task = cur.fetchone()
            if task is None:
                raise KeyError(identifier)
            cur.execute("INSERT INTO remediation_submissions (id, task_id, submitted_by, note) VALUES (%s, %s, %s, %s) RETURNING *", (submission_identifier, identifier, kwargs["submitted_by"], kwargs["note"]))
            row = cur.fetchone()
            for item in evidence:
                cur.execute("INSERT INTO remediation_evidence (id, submission_id, kind, label, uri, object_key, content_type, sha256, byte_size) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)", (remediation_evidence_id(), submission_identifier, item["kind"], item["label"], item.get("uri"), item.get("object_key"), item.get("content_type"), item.get("sha256"), item.get("byte_size")))
            cur.execute("UPDATE remediation_tasks SET status = 'pending_review', version = version + 1, updated_at = now() WHERE id = %s", (identifier,))
            conn.commit()
        result = self._remediation_submission_dict(row)
        result["evidence"] = evidence
        return result

    def get_remediation_submission(self, identifier: str) -> dict[str, Any] | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM remediation_submissions WHERE id = %s", (identifier,))
            row = cur.fetchone()
            if row is None:
                return None
            result = self._remediation_submission_dict(row)
            cur.execute("SELECT * FROM remediation_evidence WHERE submission_id = %s ORDER BY created_at", (identifier,))
            result["evidence"] = [self._remediation_evidence_dict(item) for item in cur.fetchall()]
            return result

    def review_remediation_submission(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        accepted = kwargs["decision"] == "accepted"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE remediation_submissions SET status = %s, reviewed_by = %s, review_note = %s, reviewed_at = now() WHERE id = %s AND status = 'pending_review' RETURNING *", ("accepted" if accepted else "rejected", kwargs["reviewed_by"], kwargs.get("review_note"), identifier))
            row = cur.fetchone()
            if row is None:
                raise ValueError("整改提交不存在或已经复核")
            cur.execute("UPDATE remediation_tasks SET status = %s, version = version + 1, updated_at = now() WHERE id = %s", ("completed" if accepted else "in_progress", row["task_id"]))
            cur.execute("SELECT plan_id FROM remediation_tasks WHERE id = %s", (row["task_id"],))
            plan_id = cur.fetchone()["plan_id"]
            if accepted:
                cur.execute("SELECT COUNT(*) AS count FROM remediation_tasks WHERE plan_id = %s AND status <> 'completed'", (plan_id,))
                if int(cur.fetchone()["count"]) == 0:
                    cur.execute("UPDATE remediation_plans SET status = 'completed', version = version + 1, updated_at = now() WHERE id = %s", (plan_id,))
            conn.commit()
        result = self.get_remediation_submission(identifier)
        return result or self._remediation_submission_dict(row)

    def list_remediation_events(self, identifier: str) -> list[dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM remediation_events WHERE plan_id = %s ORDER BY created_at", (identifier,))
            return [{"id": row["id"], "plan_id": row["plan_id"], "task_id": row["task_id"], "actor_id": row["actor_id"], "event_type": row["event_type"], "payload": row["payload_json"] or {}, "created_at": _json_value(row["created_at"])} for row in cur.fetchall()]

    def add_remediation_event(self, identifier: str, actor_id: str, **kwargs: Any) -> dict[str, Any]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("INSERT INTO remediation_events (id, plan_id, task_id, actor_id, event_type, payload_json) VALUES (%s, %s, %s, %s, %s, %s) RETURNING *", (remediation_event_id(), identifier, kwargs.get("task_id"), actor_id, kwargs["event_type"], _jsonb(kwargs.get("payload"))))
            row = cur.fetchone()
            conn.commit()
        return {"id": row["id"], "plan_id": row["plan_id"], "task_id": row["task_id"], "actor_id": row["actor_id"], "event_type": row["event_type"], "payload": row["payload_json"] or {}, "created_at": _json_value(row["created_at"])}

    def get_feedback(self, identifier: str) -> dict[str, Any] | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM case_feedback WHERE case_id = %s", (identifier,))
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "case_id": row["case_id"],
            "actor_id": row["actor_id"],
            "conclusion_useful": row["conclusion_useful"],
            "missing_sources": row["missing_sources"],
            "notes": row["notes"],
            "citation_verdicts": row["citation_verdicts_json"] or {},
            "updated_at": _json_value(row["updated_at"]),
        }

    def save_feedback(self, identifier: str, actor_id: str, **kwargs: Any) -> dict[str, Any]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO case_feedback (
                    case_id, actor_id, conclusion_useful, missing_sources, notes, citation_verdicts_json
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (case_id) DO UPDATE SET
                    actor_id = EXCLUDED.actor_id,
                    conclusion_useful = EXCLUDED.conclusion_useful,
                    missing_sources = EXCLUDED.missing_sources,
                    notes = EXCLUDED.notes,
                    citation_verdicts_json = EXCLUDED.citation_verdicts_json,
                    updated_at = now()
                RETURNING *
                """,
                (
                    identifier,
                    actor_id,
                    kwargs.get("conclusion_useful"),
                    kwargs.get("missing_sources", ""),
                    kwargs.get("notes", ""),
                    _jsonb(kwargs.get("citation_verdicts")),
                ),
            )
            conn.commit()
        return self.get_feedback(identifier) or {"case_id": identifier}

    def dashboard_summary(self, user: UserRecord) -> dict[str, Any]:
        cases = self.list_cases(user)
        counts = {status: 0 for status in CASE_TRANSITIONS}
        risk_counts = {"high": 0, "medium": 0, "low": 0, "insufficient_evidence": 0}
        for item in cases:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
            if item.get("risk_level") in risk_counts:
                risk_counts[item["risk_level"]] += 1
        return {
            "total_cases": len(cases),
            "status_counts": counts,
            "risk_counts": risk_counts,
            "recent_cases": cases[:5],
        }


class InMemoryCaseStore:
    """Explicit test double with the same public behavior as PostgreSQL."""

    def __init__(self, seed_password: str = "password") -> None:
        self.users: dict[str, tuple[UserRecord, str]] = {}
        self.sessions: dict[str, tuple[UserRecord, str]] = {}
        self.cases: dict[str, MemoryCase] = {}
        self.events: list[dict[str, Any]] = []
        self.remediation_plans: dict[str, dict[str, Any]] = {}
        self.remediation_tasks: dict[str, dict[str, Any]] = {}
        self.remediation_submissions: dict[str, dict[str, Any]] = {}
        self.remediation_evidence: dict[str, dict[str, Any]] = {}
        self.remediation_events: list[dict[str, Any]] = []
        self.feedback: dict[str, dict[str, Any]] = {}
        hasher = PostgresCaseStore._password_hasher
        for username, display_name, role in (
            ("requester@crosscomply.local", "业务申请人", "requester"),
            ("reviewer@crosscomply.local", "合规审核人", "reviewer"),
            ("admin@crosscomply.local", "系统管理员", "admin"),
        ):
            user = UserRecord(f"user_{uuid4().hex[:16]}", username, display_name, role)  # type: ignore[arg-type]
            self.users[username] = (user, hasher.hash(seed_password))

    def initialize(self) -> None:
        return None

    def authenticate(self, username: str, password: str) -> UserRecord | None:
        record = self.users.get(username.strip().lower())
        if record and PostgresCaseStore._password_hasher.verify(password, record[1]):
            return record[0]
        return None

    def create_session(self, user_id: str, ttl_hours: int) -> tuple[str, str]:
        user = next((record[0] for record in self.users.values() if record[0].id == user_id), None)
        if user is None:
            raise KeyError(user_id)
        token = secrets.token_urlsafe(24)
        expires = datetime.now(UTC) + timedelta(hours=ttl_hours)
        self.sessions[token] = (user, expires.isoformat())
        return token, expires.isoformat()

    def get_user_by_session(self, token: str) -> UserRecord | None:
        record = self.sessions.get(token)
        if record is None:
            return None
        if datetime.fromisoformat(record[1]) <= datetime.now(UTC):
            self.sessions.pop(token, None)
            return None
        return record[0]

    def get_user(self, user_id: str) -> UserRecord | None:
        return next((record[0] for record in self.users.values() if record[0].id == user_id), None)

    def list_assignable_users(self) -> list[UserRecord]:
        return sorted((record[0] for record in self.users.values()), key=lambda item: (item.display_name, item.username))

    def delete_session(self, token: str) -> None:
        self.sessions.pop(token, None)

    @staticmethod
    def _case_dict(item: MemoryCase) -> dict[str, Any]:
        return {
            "id": item.id,
            "case_number": item.case_number,
            "title": item.title,
            "question": item.question,
            "material_text": item.material_text,
            "material_source": item.material_source,
            "intake": item.intake,
            "status": item.status,
            "review_mode": item.review_mode,
            "rerank_mode": item.rerank_mode,
            "created_by": item.created_by,
            "owner_id": item.owner_id,
            "facts_confirmed": item.facts_confirmed,
            "risk_level": item.risk_level,
            "trace_id": item.trace_id,
            "response": item.response,
            "created_at": item.created_at,
            "updated_at": item.updated_at,
        }

    def create_case(self, **kwargs: Any) -> dict[str, Any]:
        now = utc_now()
        item = MemoryCase(
            id=kwargs.get("id") or case_id(),
            case_number=kwargs.get("case_number") or case_number(),
            title=kwargs.get("title") or kwargs["question"][:80],
            question=kwargs["question"],
            material_text=kwargs["material_text"],
            material_source=kwargs.get("material_source"),
            intake=kwargs.get("intake") or {},
            status=kwargs.get("status", "draft"),
            review_mode=kwargs.get("review_mode", "llm"),
            rerank_mode=kwargs.get("rerank_mode", "off"),
            created_by=kwargs["created_by"],
            owner_id=kwargs.get("owner_id"),
            facts_confirmed=kwargs.get("facts_confirmed", False),
            created_at=now,
            updated_at=now,
        )
        self.cases[item.id] = item
        return self._case_dict(item)

    def get_case(self, identifier: str) -> dict[str, Any] | None:
        item = self.cases.get(identifier)
        return self._case_dict(item) if item else None

    def list_cases(self, user: UserRecord, query: str | None = None) -> list[dict[str, Any]]:
        items = list(self.cases.values())
        if user.role == "requester":
            items = [item for item in items if item.created_by == user.id]
        if query and query.strip():
            needle = query.strip().lower()
            items = [
                item
                for item in items
                if needle in item.question.lower() or needle in item.id.lower()
            ]
        items.sort(key=lambda item: item.updated_at, reverse=True)
        return [self._case_dict(item) for item in items]

    def update_case(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        item = self.cases.get(identifier)
        if item is None:
            raise KeyError(identifier)
        mapping = {"intake_json": "intake", "response_json": "response"}
        for key, value in kwargs.items():
            target = mapping.get(key, key)
            if hasattr(item, target):
                setattr(item, target, value)
        item.updated_at = utc_now()
        return self._case_dict(item)

    def add_event(self, identifier: str, actor_id: str, **kwargs: Any) -> dict[str, Any]:
        event = {
            "id": event_id(),
            "case_id": identifier,
            "actor_id": actor_id,
            "event_type": kwargs["event_type"],
            "from_status": kwargs.get("from_status"),
            "to_status": kwargs.get("to_status"),
            "payload": kwargs.get("payload") or {},
            "created_at": utc_now(),
        }
        self.events.append(event)
        return event

    def list_events(self, identifier: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event["case_id"] == identifier]

    def get_remediation_plan(self, identifier: str) -> dict[str, Any] | None:
        plan = self.remediation_plans.get(identifier) if identifier.startswith("remediation_plan_") else next((item for item in self.remediation_plans.values() if item["case_id"] == identifier), None)
        if plan is None:
            return None
        result = dict(plan)
        tasks = [dict(item) for item in self.remediation_tasks.values() if item["plan_id"] == plan["id"]]
        for task in tasks:
            submissions = [dict(item) for item in self.remediation_submissions.values() if item["task_id"] == task["id"]]
            for submission in submissions:
                submission["evidence"] = [dict(item) for item in self.remediation_evidence.values() if item["submission_id"] == submission["id"]]
            task["submissions"] = submissions
        result["tasks"] = tasks
        result["events"] = [dict(item) for item in self.remediation_events if item["plan_id"] == plan["id"]]
        return result

    def list_actions(self, identifier: str) -> list[dict[str, Any]]:
        """Return remediation tasks in the report detail shape; no old table is used."""
        return [self._report_task_shape(item) for item in self.list_remediation_tasks(identifier)]

    @staticmethod
    def _report_task_shape(item: dict[str, Any]) -> dict[str, Any]:
        report_item = dict(item)
        report_item["owner_role"] = item.get("assignee_id") or "未分派"
        report_item["evidence_expected"] = item.get("acceptance_criteria") or ""
        report_item["status"] = {
            "open": "open", "in_progress": "in_progress", "pending_review": "in_progress", "completed": "completed"
        }.get(str(item.get("status")), "open")
        return report_item

    def create_remediation_plan(self, identifier: str, created_by: str, **kwargs: Any) -> dict[str, Any]:
        if any(item["case_id"] == identifier for item in self.remediation_plans.values()):
            raise ValueError("案件已经存在整改计划")
        now = utc_now()
        plan = {
            "id": kwargs.get("id") or remediation_plan_id(), "case_id": identifier,
            "created_by": created_by, "status": "draft",
            "no_remediation_reason": kwargs.get("no_remediation_reason"), "version": 1,
            "created_at": now, "updated_at": now,
        }
        self.remediation_plans[plan["id"]] = plan
        for item in kwargs.get("tasks") or []:
            self.create_remediation_task(plan["id"], case_id=identifier, **item)
        return self.get_remediation_plan(identifier) or plan

    def activate_remediation_plan(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        plan = self.remediation_plans.get(identifier)
        if plan is None:
            raise KeyError(identifier)
        if kwargs.get("expected_version") is not None and kwargs["expected_version"] != plan["version"]:
            raise ValueError("整改计划版本已变化，请刷新后重试")
        tasks = [item for item in self.remediation_tasks.values() if item["plan_id"] == identifier]
        if not tasks and not (plan.get("no_remediation_reason") or "").strip():
            raise ValueError("整改计划至少需要一项任务，或填写无需整改的理由")
        if any(not item.get("assignee_id") or not item.get("due_date") for item in tasks):
            raise ValueError("整改任务必须指定负责人和截止日期后才能激活")
        plan.update(status="completed" if not tasks else "active", version=plan["version"] + 1, updated_at=utc_now())
        return self.get_remediation_plan(plan["case_id"]) or plan

    def cancel_remediation_plan(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        plan = self.remediation_plans.get(identifier)
        if plan is None:
            raise KeyError(identifier)
        plan.update(status="cancelled", version=plan["version"] + 1, updated_at=utc_now())
        return self.get_remediation_plan(plan["case_id"]) or plan

    def list_remediation_tasks(self, case_id: str | None = None, *, assignee_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        items = list(self.remediation_tasks.values())
        if case_id:
            items = [item for item in items if item["case_id"] == case_id]
        if assignee_id:
            items = [item for item in items if item.get("assignee_id") == assignee_id]
        if status:
            items = [item for item in items if item["status"] == status]
        return [dict(item) for item in sorted(items, key=lambda item: (item.get("due_date") or "9999-12-31", item["created_at"]))]

    def get_remediation_task(self, identifier: str) -> dict[str, Any] | None:
        task = self.remediation_tasks.get(identifier)
        if task is None:
            return None
        result = dict(task)
        result["submissions"] = []
        for submission in self.remediation_submissions.values():
            if submission["task_id"] == identifier:
                item = dict(submission)
                item["evidence"] = [dict(e) for e in self.remediation_evidence.values() if e["submission_id"] == item["id"]]
                result["submissions"].append(item)
        return result

    def create_remediation_task(self, plan_id: str, **kwargs: Any) -> dict[str, Any]:
        plan = self.remediation_plans.get(plan_id)
        if plan is None:
            raise KeyError(plan_id)
        now = utc_now()
        task = {
            "id": kwargs.get("id") or remediation_task_id(), "plan_id": plan_id,
            "case_id": kwargs["case_id"], "title": kwargs["title"],
            "description": kwargs.get("description", ""), "acceptance_criteria": kwargs.get("acceptance_criteria", ""),
            "source_recommendation_index": kwargs.get("source_recommendation_index"),
            "source_recommendation": kwargs.get("source_recommendation"), "assignee_id": kwargs.get("assignee_id"),
            "priority": kwargs.get("priority", "medium"), "due_date": kwargs.get("due_date"),
            "status": "open", "version": 1, "created_at": now, "updated_at": now,
        }
        self.remediation_tasks[task["id"]] = task
        return dict(task)

    def update_remediation_task(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        task = self.remediation_tasks.get(identifier)
        if task is None:
            raise KeyError(identifier)
        if kwargs.get("expected_version") is not None and kwargs["expected_version"] != task["version"]:
            raise ValueError("整改任务版本已变化，请刷新后重试")
        for key in ("title", "description", "acceptance_criteria", "assignee_id", "priority", "due_date"):
            if key in kwargs:
                task[key] = kwargs[key]
        task["version"] += 1
        task["updated_at"] = utc_now()
        return dict(task)

    def start_remediation_task(self, identifier: str) -> dict[str, Any]:
        task = self.remediation_tasks.get(identifier)
        if task is None:
            raise KeyError(identifier)
        if task["status"] != "open":
            raise ValueError("整改任务当前不能开始")
        task.update(status="in_progress", version=task["version"] + 1, updated_at=utc_now())
        return dict(task)

    def create_remediation_submission(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        task = self.remediation_tasks.get(identifier)
        if task is None:
            raise KeyError(identifier)
        if task["status"] != "in_progress":
            raise ValueError("只有处理中的整改任务可以提交")
        now = utc_now()
        submission = {"id": remediation_submission_id(), "task_id": identifier, "submitted_by": kwargs["submitted_by"], "note": kwargs["note"], "status": "pending_review", "reviewed_by": None, "review_note": None, "reviewed_at": None, "created_at": now}
        self.remediation_submissions[submission["id"]] = submission
        for item in kwargs.get("evidence") or []:
            evidence = {"id": remediation_evidence_id(), "submission_id": submission["id"], **item, "created_at": now}
            self.remediation_evidence[evidence["id"]] = evidence
        task.update(status="pending_review", version=task["version"] + 1, updated_at=now)
        result = dict(submission)
        result["evidence"] = [dict(item) for item in self.remediation_evidence.values() if item["submission_id"] == submission["id"]]
        return result

    def get_remediation_submission(self, identifier: str) -> dict[str, Any] | None:
        submission = self.remediation_submissions.get(identifier)
        if submission is None:
            return None
        result = dict(submission)
        result["evidence"] = [dict(item) for item in self.remediation_evidence.values() if item["submission_id"] == identifier]
        return result

    def review_remediation_submission(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        submission = self.remediation_submissions.get(identifier)
        if submission is None or submission["status"] != "pending_review":
            raise ValueError("整改提交不存在或已经复核")
        accepted = kwargs["decision"] == "accepted"
        submission.update(status="accepted" if accepted else "rejected", reviewed_by=kwargs["reviewed_by"], review_note=kwargs.get("review_note"), reviewed_at=utc_now())
        task = self.remediation_tasks[submission["task_id"]]
        task.update(status="completed" if accepted else "in_progress", version=task["version"] + 1, updated_at=utc_now())
        if accepted:
            plan = self.remediation_plans[task["plan_id"]]
            if all(item["status"] == "completed" for item in self.remediation_tasks.values() if item["plan_id"] == plan["id"]):
                plan.update(status="completed", version=plan["version"] + 1, updated_at=utc_now())
        return self.get_remediation_submission(identifier) or submission

    def list_remediation_events(self, identifier: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self.remediation_events if item["plan_id"] == identifier]

    def add_remediation_event(self, identifier: str, actor_id: str, **kwargs: Any) -> dict[str, Any]:
        event = {
            "id": remediation_event_id(), "plan_id": identifier, "task_id": kwargs.get("task_id"),
            "actor_id": actor_id, "event_type": kwargs["event_type"],
            "payload": kwargs.get("payload") or {}, "created_at": utc_now(),
        }
        self.remediation_events.append(event)
        return event

    def get_feedback(self, identifier: str) -> dict[str, Any] | None:
        return self.feedback.get(identifier)

    def save_feedback(self, identifier: str, actor_id: str, **kwargs: Any) -> dict[str, Any]:
        feedback = {
            "case_id": identifier,
            "actor_id": actor_id,
            "conclusion_useful": kwargs.get("conclusion_useful"),
            "missing_sources": kwargs.get("missing_sources", ""),
            "notes": kwargs.get("notes", ""),
            "citation_verdicts": kwargs.get("citation_verdicts") or {},
            "updated_at": utc_now(),
        }
        self.feedback[identifier] = feedback
        return feedback

    def dashboard_summary(self, user: UserRecord) -> dict[str, Any]:
        cases = self.list_cases(user)
        counts = {status: 0 for status in CASE_TRANSITIONS}
        risks = {"high": 0, "medium": 0, "low": 0, "insufficient_evidence": 0}
        for item in cases:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
            if item.get("risk_level") in risks:
                risks[item["risk_level"]] += 1
        return {
            "total_cases": len(cases),
            "status_counts": counts,
            "risk_counts": risks,
            "recent_cases": cases[:5],
        }
