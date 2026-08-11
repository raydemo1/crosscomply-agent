"""Persistence and authentication primitives for the CrossComply workbench.

The production application uses PostgreSQL for all case state.  The in-memory
store is intentionally an explicit test double injected by API tests; it is
never selected implicitly at runtime.
"""

from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from pwdlib import PasswordHash

CaseStatus = Literal[
    "draft",
    "submitted",
    "in_review",
    "needs_info",
    "completed",
    "review_failed",
]
UserRole = Literal["requester", "reviewer", "admin"]
ActionStatus = Literal["open", "in_progress", "completed"]

CASE_TRANSITIONS: dict[str, set[str]] = {
    "draft": {"submitted"},
    "submitted": {"in_review", "needs_info"},
    "in_review": {"needs_info", "completed", "review_failed"},
    "needs_info": {"submitted", "in_review"},
    "completed": set(),
    "review_failed": {"in_review", "needs_info"},
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    username TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('requester', 'reviewer', 'admin')),
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS review_cases (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    question TEXT NOT NULL,
    material_text TEXT NOT NULL,
    material_source TEXT,
    intake_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL CHECK (
        status IN ('draft', 'submitted', 'in_review', 'needs_info', 'completed', 'review_failed')
    ),
    review_mode TEXT NOT NULL DEFAULT 'llm',
    rerank_mode TEXT NOT NULL DEFAULT 'off',
    created_by TEXT NOT NULL REFERENCES users(id),
    owner_id TEXT REFERENCES users(id),
    facts_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
    risk_level TEXT,
    trace_id TEXT,
    response_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS review_cases_created_by_idx ON review_cases(created_by);
CREATE INDEX IF NOT EXISTS review_cases_status_idx ON review_cases(status);
CREATE INDEX IF NOT EXISTS review_cases_updated_at_idx ON review_cases(updated_at DESC);

CREATE TABLE IF NOT EXISTS case_actions (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES review_cases(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    owner_role TEXT NOT NULL DEFAULT 'reviewer',
    priority TEXT NOT NULL DEFAULT 'medium',
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'in_progress', 'completed')),
    due_date DATE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS case_events (
    id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL REFERENCES review_cases(id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL REFERENCES users(id),
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS case_events_case_idx ON case_events(case_id, created_at);

CREATE TABLE IF NOT EXISTS case_feedback (
    case_id TEXT PRIMARY KEY REFERENCES review_cases(id) ON DELETE CASCADE,
    actor_id TEXT NOT NULL REFERENCES users(id),
    conclusion_useful BOOLEAN,
    missing_sources TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    citation_verdicts_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash TEXT UNIQUE NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS sessions_token_idx ON sessions(token_hash);
"""


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


def event_id() -> str:
    return f"event_{uuid4().hex[:16]}"


def action_id() -> str:
    return f"action_{uuid4().hex[:16]}"


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _row_case(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
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

    def list_actions(self, case_id: str) -> list[dict[str, Any]]: ...

    def create_action(self, case_id: str, **kwargs: Any) -> dict[str, Any]: ...

    def update_action(self, action_id: str, **kwargs: Any) -> dict[str, Any]: ...

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
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            conn.commit()
        seed_password = os.getenv("CROSSCOMPLY_SEED_PASSWORD", "").strip()
        if seed_password:
            self._seed_users(seed_password)

    def _seed_users(self, password: str) -> None:
        users = (
            ("requester@crosscomply.local", "业务申请人", "requester"),
            ("reviewer@crosscomply.local", "合规审核人", "reviewer"),
            ("admin@crosscomply.local", "系统管理员", "admin"),
        )
        password_hash = self._password_hasher.hash(password)
        with self._connect() as conn:
            with conn.cursor() as cur:
                for username, display_name, role in users:
                    cur.execute(
                        """
                        INSERT INTO users (id, username, display_name, role, password_hash)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (username) DO NOTHING
                        """,
                        (f"user_{uuid4().hex[:16]}", username, display_name, role, password_hash),
                    )
            conn.commit()

    def authenticate(self, username: str, password: str) -> UserRecord | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, display_name, role, password_hash FROM users WHERE username = %s",
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
                WHERE s.token_hash = %s AND s.expires_at > now()
                """,
                (hash_session_token(token),),
            )
            row = cur.fetchone()
        return _row_user(row) if row else None

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
                    id, title, question, material_text, material_source, intake_json,
                    status, review_mode, rerank_mode, created_by, owner_id,
                    facts_confirmed, created_at, updated_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    identifier,
                    kwargs.get("title") or kwargs["question"][:80],
                    kwargs["question"],
                    kwargs["material_text"],
                    kwargs.get("material_source"),
                    kwargs.get("intake") or {},
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
            "title", "question", "material_text", "material_source", "intake_json",
            "status", "review_mode", "rerank_mode", "owner_id", "facts_confirmed",
            "risk_level", "trace_id", "response_json",
        }
        updates = {key: value for key, value in kwargs.items() if key in allowed}
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
                    event_id(), identifier, actor_id, kwargs["event_type"],
                    kwargs.get("from_status"), kwargs.get("to_status"), kwargs.get("payload") or {},
                ),
            )
            row = cur.fetchone()
            conn.commit()
        return {
            "id": row["id"], "case_id": row["case_id"], "actor_id": row["actor_id"],
            "event_type": row["event_type"], "from_status": row["from_status"],
            "to_status": row["to_status"], "payload": row["payload_json"] or {},
            "created_at": _json_value(row["created_at"]),
        }

    def list_events(self, identifier: str) -> list[dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM case_events WHERE case_id = %s ORDER BY created_at", (identifier,))
            return [
                {
                    "id": row["id"], "case_id": row["case_id"], "actor_id": row["actor_id"],
                    "event_type": row["event_type"], "from_status": row["from_status"],
                    "to_status": row["to_status"], "payload": row["payload_json"] or {},
                    "created_at": _json_value(row["created_at"]),
                }
                for row in cur.fetchall()
            ]

    def list_actions(self, identifier: str) -> list[dict[str, Any]]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM case_actions WHERE case_id = %s ORDER BY created_at", (identifier,))
            return [self._action_dict(row) for row in cur.fetchall()]

    @staticmethod
    def _action_dict(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"], "case_id": row["case_id"], "title": row["title"],
            "description": row["description"], "owner_role": row["owner_role"],
            "priority": row["priority"], "status": row["status"],
            "due_date": _json_value(row["due_date"]),
            "created_at": _json_value(row["created_at"]),
            "updated_at": _json_value(row["updated_at"]),
        }

    def create_action(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO case_actions (id, case_id, title, description, owner_role, priority, status, due_date)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
                """,
                (
                    action_id(), identifier, kwargs["title"], kwargs.get("description", ""),
                    kwargs.get("owner_role", "reviewer"), kwargs.get("priority", "medium"),
                    kwargs.get("status", "open"), kwargs.get("due_date"),
                ),
            )
            row = cur.fetchone()
            conn.commit()
        return self._action_dict(row)

    def update_action(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        allowed = {"title", "description", "owner_role", "priority", "status", "due_date"}
        updates = {key: value for key, value in kwargs.items() if key in allowed}
        updates["updated_at"] = datetime.now(UTC)
        assignments = ", ".join(f"{key} = %s" for key in updates)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"UPDATE case_actions SET {assignments} WHERE id = %s RETURNING *",
                [*updates.values(), identifier],
            )
            row = cur.fetchone()
            conn.commit()
        if row is None:
            raise KeyError(identifier)
        return self._action_dict(row)

    def get_feedback(self, identifier: str) -> dict[str, Any] | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM case_feedback WHERE case_id = %s", (identifier,))
            row = cur.fetchone()
        if row is None:
            return None
        return {
            "case_id": row["case_id"], "actor_id": row["actor_id"],
            "conclusion_useful": row["conclusion_useful"],
            "missing_sources": row["missing_sources"], "notes": row["notes"],
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
                    identifier, actor_id, kwargs.get("conclusion_useful"),
                    kwargs.get("missing_sources", ""), kwargs.get("notes", ""),
                    kwargs.get("citation_verdicts") or {},
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
        self.actions: dict[str, dict[str, Any]] = {}
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

    def delete_session(self, token: str) -> None:
        self.sessions.pop(token, None)

    @staticmethod
    def _case_dict(item: MemoryCase) -> dict[str, Any]:
        return {
            "id": item.id, "title": item.title, "question": item.question,
            "material_text": item.material_text, "material_source": item.material_source,
            "intake": item.intake, "status": item.status, "review_mode": item.review_mode,
            "rerank_mode": item.rerank_mode, "created_by": item.created_by,
            "owner_id": item.owner_id, "facts_confirmed": item.facts_confirmed,
            "risk_level": item.risk_level, "trace_id": item.trace_id,
            "response": item.response, "created_at": item.created_at, "updated_at": item.updated_at,
        }

    def create_case(self, **kwargs: Any) -> dict[str, Any]:
        now = utc_now()
        item = MemoryCase(
            id=kwargs.get("id") or case_id(), title=kwargs.get("title") or kwargs["question"][:80],
            question=kwargs["question"], material_text=kwargs["material_text"],
            material_source=kwargs.get("material_source"), intake=kwargs.get("intake") or {},
            status=kwargs.get("status", "draft"), review_mode=kwargs.get("review_mode", "llm"),
            rerank_mode=kwargs.get("rerank_mode", "off"), created_by=kwargs["created_by"],
            owner_id=kwargs.get("owner_id"), facts_confirmed=kwargs.get("facts_confirmed", False),
            created_at=now, updated_at=now,
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
            items = [item for item in items if needle in item.question.lower() or needle in item.id.lower()]
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
            "id": event_id(), "case_id": identifier, "actor_id": actor_id,
            "event_type": kwargs["event_type"], "from_status": kwargs.get("from_status"),
            "to_status": kwargs.get("to_status"), "payload": kwargs.get("payload") or {},
            "created_at": utc_now(),
        }
        self.events.append(event)
        return event

    def list_events(self, identifier: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event["case_id"] == identifier]

    def list_actions(self, identifier: str) -> list[dict[str, Any]]:
        return [action for action in self.actions.values() if action["case_id"] == identifier]

    def create_action(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        now = utc_now()
        action = {
            "id": action_id(), "case_id": identifier, "title": kwargs["title"],
            "description": kwargs.get("description", ""), "owner_role": kwargs.get("owner_role", "reviewer"),
            "priority": kwargs.get("priority", "medium"), "status": kwargs.get("status", "open"),
            "due_date": kwargs.get("due_date"), "created_at": now, "updated_at": now,
        }
        self.actions[action["id"]] = action
        return action

    def update_action(self, identifier: str, **kwargs: Any) -> dict[str, Any]:
        action = self.actions.get(identifier)
        if action is None:
            raise KeyError(identifier)
        action.update({key: value for key, value in kwargs.items() if key != "id"})
        action["updated_at"] = utc_now()
        return action

    def get_feedback(self, identifier: str) -> dict[str, Any] | None:
        return self.feedback.get(identifier)

    def save_feedback(self, identifier: str, actor_id: str, **kwargs: Any) -> dict[str, Any]:
        feedback = {
            "case_id": identifier, "actor_id": actor_id,
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
        return {"total_cases": len(cases), "status_counts": counts, "risk_counts": risks, "recent_cases": cases[:5]}
