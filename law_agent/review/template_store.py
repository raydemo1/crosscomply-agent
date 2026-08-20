"""Persistence for reusable CrossComply new-case templates.

Templates intentionally contain only field presets used while creating a case. They
never carry material text, review output, citations, or audit events.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from law_agent.review.case_store import UserRecord, utc_now


def template_id() -> str:
    return f"template_{uuid4().hex[:16]}"


def _json_value(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _template_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "description": row.get("description") or "",
        "question": row["question"],
        "intake": row.get("intake_json") or {},
        "review_mode": row.get("review_mode") or "llm",
        "rerank_mode": row.get("rerank_mode") or "off",
        "created_by": row["created_by"],
        "updated_by": row.get("updated_by"),
        "archived": bool(row.get("archived", False)),
        "created_at": _json_value(row["created_at"]),
        "updated_at": _json_value(row["updated_at"]),
    }


class TemplateStore(Protocol):
    def initialize(self) -> None: ...

    def list_templates(self, user: UserRecord, query: str | None = None) -> list[dict[str, Any]]: ...

    def create_template(self, user: UserRecord, **kwargs: Any) -> dict[str, Any]: ...

    def update_template(self, template_id: str, user: UserRecord, **kwargs: Any) -> dict[str, Any]: ...

    def archive_template(self, template_id: str, user: UserRecord) -> dict[str, Any]: ...


class PostgresTemplateStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def initialize(self) -> None:
        # The schema is owned by Alembic. This check catches an un-upgraded service
        # early while keeping startup behavior consistent with CaseStore.
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT version_num FROM alembic_version WHERE version_num = %s",
                ("0003_case_templates",),
            )
            if cur.fetchone() is None:
                raise RuntimeError("数据库未应用 CrossComply 使用模板结构，请先运行 alembic upgrade head")

    @staticmethod
    def _can_manage(user: UserRecord, row: dict[str, Any]) -> bool:
        return user.role == "admin" or row.get("created_by") == user.id

    def list_templates(self, user: UserRecord, query: str | None = None) -> list[dict[str, Any]]:
        conditions = ["archived = FALSE"]
        params: list[Any] = []
        if query and query.strip():
            conditions.append("(name ILIKE %s OR description ILIKE %s OR question ILIKE %s)")
            pattern = f"%{query.strip()}%"
            params.extend([pattern, pattern, pattern])
        where = " AND ".join(conditions)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT * FROM case_templates WHERE {where} ORDER BY updated_at DESC, name",
                params,
            )
            return [_template_dict(row) for row in cur.fetchall()]

    def create_template(self, user: UserRecord, **kwargs: Any) -> dict[str, Any]:
        identifier = kwargs.get("id") or template_id()
        now = datetime.now(UTC)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO case_templates
                    (id, name, description, question, intake_json, review_mode, rerank_mode,
                     created_by, updated_by, archived, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s, %s)
                RETURNING *
                """,
                (
                    identifier,
                    kwargs["name"],
                    kwargs.get("description", ""),
                    kwargs["question"],
                    Jsonb(kwargs.get("intake") or {}),
                    kwargs.get("review_mode", "llm"),
                    kwargs.get("rerank_mode", "off"),
                    user.id,
                    user.id,
                    now,
                    now,
                ),
            )
            row = cur.fetchone()
            conn.commit()
        return _template_dict(row)

    def update_template(self, identifier: str, user: UserRecord, **kwargs: Any) -> dict[str, Any]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM case_templates WHERE id = %s", (identifier,))
            existing = cur.fetchone()
            if existing is None:
                raise KeyError(identifier)
            if not self._can_manage(user, existing):
                raise PermissionError("只有模板创建者或管理员可以编辑使用模板")
            allowed = {"name", "description", "question", "intake", "review_mode", "rerank_mode", "archived"}
            updates = {key: value for key, value in kwargs.items() if key in allowed}
            if "intake" in updates:
                updates["intake_json"] = Jsonb(updates.pop("intake") or {})
            updates["updated_by"] = user.id
            updates["updated_at"] = datetime.now(UTC)
            assignments = ", ".join(f"{key} = %s" for key in updates)
            values = list(updates.values()) + [identifier]
            cur.execute(f"UPDATE case_templates SET {assignments} WHERE id = %s RETURNING *", values)
            row = cur.fetchone()
            conn.commit()
        return _template_dict(row)

    def archive_template(self, identifier: str, user: UserRecord) -> dict[str, Any]:
        return self.update_template(identifier, user, archived=True)


class InMemoryTemplateStore:
    """Explicit in-memory store used by API tests and local previews."""

    def __init__(self) -> None:
        self.templates: dict[str, dict[str, Any]] = {}

    def initialize(self) -> None:
        return None

    @staticmethod
    def _can_manage(user: UserRecord, item: dict[str, Any]) -> bool:
        return user.role == "admin" or item.get("created_by") == user.id

    def list_templates(self, user: UserRecord, query: str | None = None) -> list[dict[str, Any]]:
        items = [item for item in self.templates.values() if not item.get("archived")]
        if query and query.strip():
            needle = query.strip().lower()
            items = [
                item for item in items
                if needle in item["name"].lower()
                or needle in item["description"].lower()
                or needle in item["question"].lower()
            ]
        return sorted((dict(item) for item in items), key=lambda item: item["updated_at"], reverse=True)

    def create_template(self, user: UserRecord, **kwargs: Any) -> dict[str, Any]:
        now = utc_now()
        item = {
            "id": kwargs.get("id") or template_id(),
            "name": kwargs["name"],
            "description": kwargs.get("description", ""),
            "question": kwargs["question"],
            "intake": dict(kwargs.get("intake") or {}),
            "review_mode": kwargs.get("review_mode", "llm"),
            "rerank_mode": kwargs.get("rerank_mode", "off"),
            "created_by": user.id,
            "updated_by": user.id,
            "archived": False,
            "created_at": now,
            "updated_at": now,
        }
        self.templates[item["id"]] = item
        return dict(item)

    def update_template(self, identifier: str, user: UserRecord, **kwargs: Any) -> dict[str, Any]:
        item = self.templates.get(identifier)
        if item is None:
            raise KeyError(identifier)
        if not self._can_manage(user, item):
            raise PermissionError("只有模板创建者或管理员可以编辑使用模板")
        for key in ("name", "description", "question", "intake", "review_mode", "rerank_mode", "archived"):
            if key in kwargs:
                item[key] = dict(kwargs[key]) if key == "intake" else kwargs[key]
        item["updated_by"] = user.id
        item["updated_at"] = utc_now()
        return dict(item)

    def archive_template(self, identifier: str, user: UserRecord) -> dict[str, Any]:
        return self.update_template(identifier, user, archived=True)
