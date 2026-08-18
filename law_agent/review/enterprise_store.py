"""Persistent-workflow records and an explicit in-memory test double."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from law_agent.review.case_store import utc_now

ParseStatus = Literal["pending", "parsing", "ready", "failed"]
TaskStatus = Literal["queued", "running", "succeeded", "failed"]
DEFAULT_TASK_LEASE_SECONDS = 2 * 60 * 60


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_copy(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value))


def _timestamp(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


@dataclass(frozen=True)
class MaterialVersion:
    id: str
    material_id: str
    case_id: str
    logical_name: str
    version_number: int
    filename: str
    content_type: str
    object_key: str
    sha256: str
    byte_size: int
    uploaded_by: str
    parse_status: ParseStatus = "pending"
    parser: str | None = None
    parser_version: str | None = None
    parsed_text: str | None = None
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class MaterialSnapshot:
    id: str
    case_id: str
    fingerprint: str
    version_ids: tuple[str, ...]
    created_by: str
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class RuleSnapshot:
    id: str
    case_id: str
    material_snapshot_id: str
    ruleset_version: str
    facts: dict[str, Any]
    determination: dict[str, Any]
    created_at: str = field(default_factory=utc_now)


@dataclass
class TaskAttempt:
    attempt_number: int
    worker_id: str
    status: Literal["running", "succeeded", "failed"] = "running"
    failed_node: str | None = None
    error_category: str | None = None
    error_message: str | None = None
    started_at: str = field(default_factory=utc_now)
    finished_at: str | None = None


@dataclass
class ReviewTask:
    id: str
    case_id: str
    material_snapshot_id: str
    rule_snapshot_id: str
    idempotency_key: str
    model_id: str
    data_boundary_summary: dict[str, Any]
    status: TaskStatus = "queued"
    current_node: str | None = None
    error_category: str | None = None
    error_message: str | None = None
    attempt_count: int = 0
    result: dict[str, Any] | None = None
    attempts: list[TaskAttempt] = field(default_factory=list)
    lease_expires_at: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


class InMemoryEnterpriseStore:
    """Thread-safe test double for material snapshots and review tasks."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.material_versions: dict[str, MaterialVersion] = {}
        self.material_ids: dict[tuple[str, str], str] = {}
        self.snapshots: dict[str, MaterialSnapshot] = {}
        self.snapshot_by_fingerprint: dict[tuple[str, str], str] = {}
        self.rule_snapshots: dict[str, RuleSnapshot] = {}
        self.tasks: dict[str, ReviewTask] = {}
        self.task_by_key: dict[str, str] = {}

    def create_material_version(
        self,
        *,
        case_id: str,
        logical_name: str,
        filename: str,
        content_type: str,
        object_key: str,
        sha256: str,
        byte_size: int,
        uploaded_by: str,
        parse_status: ParseStatus = "pending",
        parser: str | None = None,
        parser_version: str | None = None,
        parsed_text: str | None = None,
    ) -> MaterialVersion:
        with self._lock:
            material_key = (case_id, logical_name)
            material_id = self.material_ids.setdefault(material_key, _identifier("material"))
            prior_versions = [
                version
                for version in self.material_versions.values()
                if version.material_id == material_id
            ]
            version = MaterialVersion(
                id=_identifier("material_version"),
                material_id=material_id,
                case_id=case_id,
                logical_name=logical_name,
                version_number=len(prior_versions) + 1,
                filename=filename,
                content_type=content_type,
                object_key=object_key,
                sha256=sha256,
                byte_size=byte_size,
                uploaded_by=uploaded_by,
                parse_status=parse_status,
                parser=parser,
                parser_version=parser_version,
                parsed_text=parsed_text,
            )
            self.material_versions[version.id] = version
            return version

    def create_material_snapshot(
        self,
        *,
        case_id: str,
        version_ids: list[str],
        created_by: str,
    ) -> MaterialSnapshot:
        with self._lock:
            if not version_ids:
                raise ValueError("材料快照至少需要一个材料版本")
            versions = []
            for version_id in version_ids:
                version = self.material_versions.get(version_id)
                if version is None or version.case_id != case_id:
                    raise ValueError(f"材料版本不属于案件：{version_id}")
                versions.append(version)
            ordered = tuple(
                sorted(versions, key=lambda item: (item.logical_name, item.version_number))
            )
            fingerprint = _canonical_hash(
                [{"version_id": item.id, "sha256": item.sha256} for item in ordered]
            )
            existing_id = self.snapshot_by_fingerprint.get((case_id, fingerprint))
            if existing_id is not None:
                return self.snapshots[existing_id]
            snapshot = MaterialSnapshot(
                id=_identifier("material_snapshot"),
                case_id=case_id,
                fingerprint=fingerprint,
                version_ids=tuple(item.id for item in ordered),
                created_by=created_by,
            )
            self.snapshots[snapshot.id] = snapshot
            self.snapshot_by_fingerprint[(case_id, fingerprint)] = snapshot.id
            return snapshot

    def get_material_version(self, version_id: str) -> MaterialVersion | None:
        with self._lock:
            return self.material_versions.get(version_id)

    def list_material_versions(self, case_id: str) -> list[MaterialVersion]:
        with self._lock:
            return sorted(
                (item for item in self.material_versions.values() if item.case_id == case_id),
                key=lambda item: (item.logical_name, item.version_number),
            )

    def create_rule_snapshot(
        self,
        *,
        case_id: str,
        material_snapshot_id: str,
        ruleset_version: str,
        facts: dict[str, Any],
        determination: dict[str, Any],
    ) -> RuleSnapshot:
        with self._lock:
            snapshot = self.snapshots.get(material_snapshot_id)
            if snapshot is None or snapshot.case_id != case_id:
                raise ValueError("规则快照必须绑定本案件的材料快照")
            rule_snapshot = RuleSnapshot(
                id=_identifier("rule_snapshot"),
                case_id=case_id,
                material_snapshot_id=material_snapshot_id,
                ruleset_version=ruleset_version,
                facts=json.loads(json.dumps(facts)),
                determination=json.loads(json.dumps(determination)),
            )
            self.rule_snapshots[rule_snapshot.id] = rule_snapshot
            return rule_snapshot

    def get_latest_material_snapshot(self, case_id: str) -> MaterialSnapshot | None:
        with self._lock:
            matches = [item for item in self.snapshots.values() if item.case_id == case_id]
            return max(matches, key=lambda item: (item.created_at, item.id), default=None)

    def get_material_snapshot(self, snapshot_id: str) -> MaterialSnapshot | None:
        with self._lock:
            return self.snapshots.get(snapshot_id)

    def get_latest_rule_snapshot(
        self, *, case_id: str, material_snapshot_id: str
    ) -> RuleSnapshot | None:
        with self._lock:
            matches = [
                item
                for item in self.rule_snapshots.values()
                if item.case_id == case_id and item.material_snapshot_id == material_snapshot_id
            ]
            return max(matches, key=lambda item: (item.created_at, item.id), default=None)

    def get_rule_snapshot(self, rule_snapshot_id: str) -> RuleSnapshot | None:
        with self._lock:
            return self.rule_snapshots.get(rule_snapshot_id)

    def enqueue_review_task(
        self,
        *,
        case_id: str,
        material_snapshot_id: str,
        rule_snapshot_id: str,
        model_id: str,
        data_boundary_summary: dict[str, Any],
    ) -> ReviewTask:
        with self._lock:
            rule_snapshot = self.rule_snapshots.get(rule_snapshot_id)
            if (
                rule_snapshot is None
                or rule_snapshot.case_id != case_id
                or rule_snapshot.material_snapshot_id != material_snapshot_id
            ):
                raise ValueError("审查任务的案件、材料快照与规则快照必须一致")
            idempotency_key = _canonical_hash(
                {
                    "case_id": case_id,
                    "material_snapshot_id": material_snapshot_id,
                    "rule_snapshot_id": rule_snapshot_id,
                    "model_id": model_id,
                    "data_boundary_summary": data_boundary_summary,
                }
            )
            existing_id = self.task_by_key.get(idempotency_key)
            if existing_id is not None:
                return self.tasks[existing_id]
            if any(
                item.case_id == case_id and item.status in {"queued", "running"}
                for item in self.tasks.values()
            ):
                raise ValueError("案件已有排队中或运行中的审查任务")
            task = ReviewTask(
                id=_identifier("review_task"),
                case_id=case_id,
                material_snapshot_id=material_snapshot_id,
                rule_snapshot_id=rule_snapshot_id,
                idempotency_key=idempotency_key,
                model_id=model_id,
                data_boundary_summary=json.loads(json.dumps(data_boundary_summary)),
            )
            self.tasks[task.id] = task
            self.task_by_key[idempotency_key] = task.id
            return task

    def claim_next_task(
        self, *, worker_id: str, lease_seconds: int = DEFAULT_TASK_LEASE_SECONDS
    ) -> ReviewTask | None:
        with self._lock:
            self._requeue_expired_tasks_locked()
            queued = sorted(
                (task for task in self.tasks.values() if task.status == "queued"),
                key=lambda task: (task.created_at, task.id),
            )
            if not queued:
                return None
            task = queued[0]
            task.status = "running"
            task.attempt_count += 1
            task.error_category = None
            task.error_message = None
            task.lease_expires_at = (
                datetime.now(UTC) + timedelta(seconds=lease_seconds)
            ).isoformat()
            task.attempts.append(
                TaskAttempt(attempt_number=task.attempt_count, worker_id=worker_id)
            )
            task.updated_at = utc_now()
            return task

    def fail_task(
        self,
        task_id: str,
        *,
        failed_node: str,
        error_category: str,
        error_message: str,
    ) -> ReviewTask:
        with self._lock:
            task = self.tasks[task_id]
            if task.status != "running" or not task.attempts:
                raise ValueError("只有运行中的审查任务可以标记失败")
            task.status = "failed"
            task.lease_expires_at = None
            task.current_node = failed_node
            task.error_category = error_category
            task.error_message = error_message
            attempt = task.attempts[-1]
            attempt.status = "failed"
            attempt.failed_node = failed_node
            attempt.error_category = error_category
            attempt.error_message = error_message
            attempt.finished_at = utc_now()
            task.updated_at = attempt.finished_at
            return task

    def retry_task(self, task_id: str) -> ReviewTask:
        with self._lock:
            task = self.tasks[task_id]
            if task.status != "failed":
                raise ValueError("只有失败的审查任务可以重试")
            task.status = "queued"
            task.lease_expires_at = None
            task.updated_at = utc_now()
            return task

    def get_task(self, task_id: str) -> ReviewTask | None:
        with self._lock:
            return self.tasks.get(task_id)

    def get_latest_task(self, case_id: str) -> ReviewTask | None:
        with self._lock:
            matches = [item for item in self.tasks.values() if item.case_id == case_id]
            return max(matches, key=lambda item: (item.created_at, item.id), default=None)

    def complete_task(
        self,
        task_id: str,
        *,
        result: dict[str, Any],
        final_node: str,
    ) -> ReviewTask:
        with self._lock:
            task = self.tasks[task_id]
            if task.status != "running" or not task.attempts:
                raise ValueError("只有运行中的审查任务可以标记完成")
            task.status = "succeeded"
            task.lease_expires_at = None
            task.current_node = final_node
            task.result = _json_copy(result)
            attempt = task.attempts[-1]
            attempt.status = "succeeded"
            attempt.finished_at = utc_now()
            task.updated_at = attempt.finished_at
            return task

    def requeue_expired_tasks(self) -> int:
        with self._lock:
            return self._requeue_expired_tasks_locked()

    def _requeue_expired_tasks_locked(self) -> int:
        now = datetime.now(UTC)
        recovered = 0
        for task in self.tasks.values():
            if task.status != "running" or task.lease_expires_at is None:
                continue
            if datetime.fromisoformat(task.lease_expires_at) > now:
                continue
            task.status = "queued"
            task.current_node = "worker_lease"
            task.error_category = "worker_lease_expired"
            task.error_message = "worker 租约过期，任务已自动重新排队"
            task.lease_expires_at = None
            task.updated_at = utc_now()
            if task.attempts and task.attempts[-1].status == "running":
                attempt = task.attempts[-1]
                attempt.status = "failed"
                attempt.failed_node = "worker_lease"
                attempt.error_category = "worker_lease_expired"
                attempt.error_message = task.error_message
                attempt.finished_at = task.updated_at
            recovered += 1
        return recovered


class PostgresEnterpriseStore:
    """PostgreSQL store for immutable inputs and durable review jobs."""

    def __init__(
        self,
        dsn: str,
        *,
        connect: Callable[[], Any] | None = None,
    ) -> None:
        self.dsn = dsn
        self._connection_factory = connect

    def _connect(self) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory()
        return psycopg.connect(self.dsn, row_factory=dict_row)

    def create_material_version(
        self,
        *,
        case_id: str,
        logical_name: str,
        filename: str,
        content_type: str,
        object_key: str,
        sha256: str,
        byte_size: int,
        uploaded_by: str,
        parse_status: ParseStatus = "pending",
        parser: str | None = None,
        parser_version: str | None = None,
        parsed_text: str | None = None,
    ) -> MaterialVersion:
        material_id = _identifier("material")
        version_id = _identifier("material_version")
        with self._connect() as conn, conn.cursor() as cur:
            # The no-op UPDATE acquires the existing material row lock. Version
            # allocation below is consequently serialized per logical material.
            cur.execute(
                """
                INSERT INTO materials (id, case_id, logical_name, created_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (case_id, logical_name) DO UPDATE
                SET logical_name = EXCLUDED.logical_name
                RETURNING id
                """,
                (material_id, case_id, logical_name, uploaded_by),
            )
            material_row = cur.fetchone()
            assert material_row is not None
            material_id = material_row["id"]
            cur.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1 AS version_number
                FROM material_versions
                WHERE material_id = %s
                """,
                (material_id,),
            )
            number_row = cur.fetchone()
            assert number_row is not None
            version_number = int(number_row["version_number"])
            cur.execute(
                """
                INSERT INTO material_versions (
                    id, material_id, version_number, filename, content_type,
                    object_key, sha256, byte_size, uploaded_by, parse_status,
                    parser, parser_version, parsed_text
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    version_id,
                    material_id,
                    version_number,
                    filename,
                    content_type,
                    object_key,
                    sha256,
                    byte_size,
                    uploaded_by,
                    parse_status,
                    parser,
                    parser_version,
                    parsed_text,
                ),
            )
            row = cur.fetchone()
            assert row is not None
            conn.commit()
        return self._material_version(row, case_id=case_id, logical_name=logical_name)

    def create_material_snapshot(
        self,
        *,
        case_id: str,
        version_ids: list[str],
        created_by: str,
    ) -> MaterialSnapshot:
        if not version_ids:
            raise ValueError("材料快照至少需要一个材料版本")
        if len(version_ids) != len(set(version_ids)):
            raise ValueError("材料快照不能重复包含同一个材料版本")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT mv.*, m.case_id, m.logical_name
                FROM material_versions mv
                JOIN materials m ON m.id = mv.material_id
                WHERE mv.id = ANY(%s)
                """,
                (version_ids,),
            )
            rows = cur.fetchall()
            if len(rows) != len(version_ids) or any(row["case_id"] != case_id for row in rows):
                invalid = next(
                    (
                        version_id
                        for version_id in version_ids
                        if not any(
                            row["id"] == version_id and row["case_id"] == case_id for row in rows
                        )
                    ),
                    version_ids[0],
                )
                raise ValueError(f"材料版本不属于案件：{invalid}")
            ordered = sorted(rows, key=lambda row: (row["logical_name"], row["version_number"]))
            fingerprint = _canonical_hash(
                [{"version_id": row["id"], "sha256": row["sha256"]} for row in ordered]
            )
            cur.execute(
                """
                INSERT INTO material_snapshots (id, case_id, fingerprint, created_by)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (case_id, fingerprint) DO UPDATE
                SET fingerprint = EXCLUDED.fingerprint
                RETURNING *
                """,
                (_identifier("material_snapshot"), case_id, fingerprint, created_by),
            )
            snapshot_row = cur.fetchone()
            assert snapshot_row is not None
            for position, row in enumerate(ordered):
                cur.execute(
                    """
                    INSERT INTO material_snapshot_versions (
                        snapshot_id, material_version_id, position
                    ) VALUES (%s, %s, %s)
                    ON CONFLICT (snapshot_id, material_version_id) DO NOTHING
                    """,
                    (snapshot_row["id"], row["id"], position),
                )
            conn.commit()
        return MaterialSnapshot(
            id=snapshot_row["id"],
            case_id=snapshot_row["case_id"],
            fingerprint=snapshot_row["fingerprint"],
            version_ids=tuple(row["id"] for row in ordered),
            created_by=snapshot_row["created_by"],
            created_at=_timestamp(snapshot_row["created_at"]),
        )

    def get_material_version(self, version_id: str) -> MaterialVersion | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT mv.*, m.case_id, m.logical_name
                FROM material_versions mv
                JOIN materials m ON m.id = mv.material_id
                WHERE mv.id = %s
                """,
                (version_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return self._material_version(row, case_id=row["case_id"], logical_name=row["logical_name"])

    def list_material_versions(self, case_id: str) -> list[MaterialVersion]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT mv.*, m.case_id, m.logical_name
                FROM material_versions mv
                JOIN materials m ON m.id = mv.material_id
                WHERE m.case_id = %s
                ORDER BY m.logical_name, mv.version_number
                """,
                (case_id,),
            )
            rows = cur.fetchall()
        return [
            self._material_version(row, case_id=row["case_id"], logical_name=row["logical_name"])
            for row in rows
        ]

    def create_rule_snapshot(
        self,
        *,
        case_id: str,
        material_snapshot_id: str,
        ruleset_version: str,
        facts: dict[str, Any],
        determination: dict[str, Any],
    ) -> RuleSnapshot:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT case_id FROM material_snapshots WHERE id = %s",
                (material_snapshot_id,),
            )
            snapshot = cur.fetchone()
            if snapshot is None or snapshot["case_id"] != case_id:
                raise ValueError("规则快照必须绑定本案件的材料快照")
            cur.execute(
                """
                INSERT INTO rule_snapshots (
                    id, case_id, material_snapshot_id, ruleset_version,
                    facts_json, determination_json
                ) VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    _identifier("rule_snapshot"),
                    case_id,
                    material_snapshot_id,
                    ruleset_version,
                    Jsonb(facts),
                    Jsonb(determination),
                ),
            )
            row = cur.fetchone()
            assert row is not None
            conn.commit()
        return RuleSnapshot(
            id=row["id"],
            case_id=row["case_id"],
            material_snapshot_id=row["material_snapshot_id"],
            ruleset_version=row["ruleset_version"],
            facts=_json_copy(row["facts_json"]),
            determination=_json_copy(row["determination_json"]),
            created_at=_timestamp(row["created_at"]),
        )

    def get_latest_material_snapshot(self, case_id: str) -> MaterialSnapshot | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM material_snapshots
                WHERE case_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (case_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                """
                SELECT material_version_id
                FROM material_snapshot_versions
                WHERE snapshot_id = %s
                ORDER BY position
                """,
                (row["id"],),
            )
            version_ids = tuple(item["material_version_id"] for item in cur.fetchall())
        return MaterialSnapshot(
            id=row["id"],
            case_id=row["case_id"],
            fingerprint=row["fingerprint"],
            version_ids=version_ids,
            created_by=row["created_by"],
            created_at=_timestamp(row["created_at"]),
        )

    def get_material_snapshot(self, snapshot_id: str) -> MaterialSnapshot | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM material_snapshots WHERE id = %s", (snapshot_id,))
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                """
                SELECT material_version_id
                FROM material_snapshot_versions
                WHERE snapshot_id = %s
                ORDER BY position
                """,
                (snapshot_id,),
            )
            version_ids = tuple(item["material_version_id"] for item in cur.fetchall())
        return MaterialSnapshot(
            id=row["id"],
            case_id=row["case_id"],
            fingerprint=row["fingerprint"],
            version_ids=version_ids,
            created_by=row["created_by"],
            created_at=_timestamp(row["created_at"]),
        )

    def get_latest_rule_snapshot(
        self, *, case_id: str, material_snapshot_id: str
    ) -> RuleSnapshot | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM rule_snapshots
                WHERE case_id = %s AND material_snapshot_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (case_id, material_snapshot_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return RuleSnapshot(
            id=row["id"],
            case_id=row["case_id"],
            material_snapshot_id=row["material_snapshot_id"],
            ruleset_version=row["ruleset_version"],
            facts=_json_copy(row["facts_json"]),
            determination=_json_copy(row["determination_json"]),
            created_at=_timestamp(row["created_at"]),
        )

    def get_rule_snapshot(self, rule_snapshot_id: str) -> RuleSnapshot | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM rule_snapshots WHERE id = %s", (rule_snapshot_id,))
            row = cur.fetchone()
        if row is None:
            return None
        return RuleSnapshot(
            id=row["id"],
            case_id=row["case_id"],
            material_snapshot_id=row["material_snapshot_id"],
            ruleset_version=row["ruleset_version"],
            facts=_json_copy(row["facts_json"]),
            determination=_json_copy(row["determination_json"]),
            created_at=_timestamp(row["created_at"]),
        )

    def enqueue_review_task(
        self,
        *,
        case_id: str,
        material_snapshot_id: str,
        rule_snapshot_id: str,
        model_id: str,
        data_boundary_summary: dict[str, Any],
    ) -> ReviewTask:
        idempotency_key = _canonical_hash(
            {
                "case_id": case_id,
                "material_snapshot_id": material_snapshot_id,
                "rule_snapshot_id": rule_snapshot_id,
                "model_id": model_id,
                "data_boundary_summary": data_boundary_summary,
            }
        )
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT id FROM review_cases WHERE id = %s FOR UPDATE", (case_id,))
            if cur.fetchone() is None:
                raise ValueError("审查任务绑定的案件不存在")
            cur.execute(
                """
                SELECT case_id, material_snapshot_id
                FROM rule_snapshots
                WHERE id = %s
                """,
                (rule_snapshot_id,),
            )
            rule = cur.fetchone()
            if (
                rule is None
                or rule["case_id"] != case_id
                or rule["material_snapshot_id"] != material_snapshot_id
            ):
                raise ValueError("审查任务的案件、材料快照与规则快照必须一致")
            cur.execute(
                """
                SELECT id FROM review_tasks
                WHERE case_id = %s AND status IN ('queued', 'running')
                  AND idempotency_key <> %s
                FOR UPDATE
                """,
                (case_id, idempotency_key),
            )
            if cur.fetchone() is not None:
                raise ValueError("案件已有排队中或运行中的审查任务")
            cur.execute(
                """
                INSERT INTO review_tasks (
                    id, case_id, material_snapshot_id, rule_snapshot_id,
                    idempotency_key, model_id, data_boundary_summary_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING *
                """,
                (
                    _identifier("review_task"),
                    case_id,
                    material_snapshot_id,
                    rule_snapshot_id,
                    idempotency_key,
                    model_id,
                    Jsonb(data_boundary_summary),
                ),
            )
            row = cur.fetchone()
            assert row is not None
            attempts = self._load_attempts(cur, row["id"])
            conn.commit()
        return self._review_task(row, attempts)

    def claim_next_task(
        self, *, worker_id: str, lease_seconds: int = DEFAULT_TASK_LEASE_SECONDS
    ) -> ReviewTask | None:
        with self._connect() as conn, conn.cursor() as cur:
            self._requeue_expired_tasks(cur)
            cur.execute(
                """
                WITH candidate AS (
                    SELECT id
                    FROM review_tasks
                    WHERE status = 'queued'
                    ORDER BY created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT 1
                )
                UPDATE review_tasks AS task
                SET status = 'running',
                    attempt_count = task.attempt_count + 1,
                    error_category = NULL,
                    error_message = NULL,
                    lease_expires_at = now() + (%s * interval '1 second'),
                    updated_at = now()
                FROM candidate
                WHERE task.id = candidate.id
                RETURNING task.*
                """,
                (lease_seconds,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                """
                INSERT INTO review_task_attempts (
                    id, task_id, attempt_number, worker_id, status
                ) VALUES (%s, %s, %s, %s, 'running')
                """,
                (_identifier("task_attempt"), row["id"], row["attempt_count"], worker_id),
            )
            attempts = self._load_attempts(cur, row["id"])
            conn.commit()
        return self._review_task(row, attempts)

    def get_task(self, task_id: str) -> ReviewTask | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM review_tasks WHERE id = %s", (task_id,))
            row = cur.fetchone()
            if row is None:
                return None
            attempts = self._load_attempts(cur, task_id)
        return self._review_task(row, attempts)

    def get_latest_task(self, case_id: str) -> ReviewTask | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM review_tasks
                WHERE case_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (case_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            attempts = self._load_attempts(cur, row["id"])
        return self._review_task(row, attempts)

    def fail_task(
        self,
        task_id: str,
        *,
        failed_node: str,
        error_category: str,
        error_message: str,
    ) -> ReviewTask:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE review_tasks
                SET status = 'failed', current_node = %s, error_category = %s,
                    error_message = %s, lease_expires_at = NULL, updated_at = now()
                WHERE id = %s AND status = 'running'
                RETURNING *
                """,
                (failed_node, error_category, error_message, task_id),
            )
            row = cur.fetchone()
            if row is None:
                self._raise_invalid_state(cur, task_id, "只有运行中的审查任务可以标记失败")
            assert row is not None
            cur.execute(
                """
                UPDATE review_task_attempts
                SET status = 'failed', failed_node = %s, error_category = %s,
                    error_message = %s, finished_at = now()
                WHERE task_id = %s AND attempt_number = %s AND status = 'running'
                """,
                (failed_node, error_category, error_message, task_id, row["attempt_count"]),
            )
            attempts = self._load_attempts(cur, task_id)
            conn.commit()
        return self._review_task(row, attempts)

    def retry_task(self, task_id: str) -> ReviewTask:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE review_tasks
                SET status = 'queued', lease_expires_at = NULL, updated_at = now()
                WHERE id = %s AND status = 'failed'
                RETURNING *
                """,
                (task_id,),
            )
            row = cur.fetchone()
            if row is None:
                self._raise_invalid_state(cur, task_id, "只有失败的审查任务可以重试")
            assert row is not None
            attempts = self._load_attempts(cur, task_id)
            conn.commit()
        return self._review_task(row, attempts)

    def complete_task(
        self,
        task_id: str,
        *,
        result: dict[str, Any],
        final_node: str,
    ) -> ReviewTask:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE review_tasks
                SET status = 'succeeded', current_node = %s, result_json = %s,
                    error_category = NULL, error_message = NULL,
                    lease_expires_at = NULL, updated_at = now()
                WHERE id = %s AND status = 'running'
                RETURNING *
                """,
                (final_node, Jsonb(result), task_id),
            )
            row = cur.fetchone()
            if row is None:
                self._raise_invalid_state(cur, task_id, "只有运行中的审查任务可以标记完成")
            assert row is not None
            cur.execute(
                """
                UPDATE review_task_attempts
                SET status = 'succeeded', finished_at = now()
                WHERE task_id = %s AND attempt_number = %s AND status = 'running'
                """,
                (task_id, row["attempt_count"]),
            )
            attempts = self._load_attempts(cur, task_id)
            conn.commit()
        return self._review_task(row, attempts)

    def requeue_expired_tasks(self) -> int:
        with self._connect() as conn, conn.cursor() as cur:
            recovered = self._requeue_expired_tasks(cur)
            conn.commit()
            return recovered

    @staticmethod
    def _requeue_expired_tasks(cur: Any) -> int:
        cur.execute(
            """
            WITH expired AS (
                UPDATE review_tasks
                SET status = 'queued', current_node = 'worker_lease',
                    error_category = 'worker_lease_expired',
                    error_message = 'worker 租约过期，任务已自动重新排队',
                    lease_expires_at = NULL, updated_at = now()
                WHERE status = 'running' AND lease_expires_at < now()
                RETURNING id, attempt_count
            )
            UPDATE review_task_attempts AS attempt
            SET status = 'failed', failed_node = 'worker_lease',
                error_category = 'worker_lease_expired',
                error_message = 'worker 租约过期，任务已自动重新排队',
                finished_at = now()
            FROM expired
            WHERE attempt.task_id = expired.id
              AND attempt.attempt_number = expired.attempt_count
              AND attempt.status = 'running'
            RETURNING expired.id
            """
        )
        return len(cur.fetchall())

    @staticmethod
    def _raise_invalid_state(cur: Any, task_id: str, message: str) -> None:
        cur.execute("SELECT status FROM review_tasks WHERE id = %s", (task_id,))
        if cur.fetchone() is None:
            raise KeyError(task_id)
        raise ValueError(message)

    @staticmethod
    def _load_attempts(cur: Any, task_id: str) -> list[TaskAttempt]:
        cur.execute(
            """
            SELECT attempt_number, worker_id, status, failed_node,
                   error_category, error_message, started_at, finished_at
            FROM review_task_attempts
            WHERE task_id = %s
            ORDER BY attempt_number
            """,
            (task_id,),
        )
        return [
            TaskAttempt(
                attempt_number=row["attempt_number"],
                worker_id=row["worker_id"],
                status=row["status"],
                failed_node=row["failed_node"],
                error_category=row["error_category"],
                error_message=row["error_message"],
                started_at=_timestamp(row["started_at"]),
                finished_at=(
                    _timestamp(row["finished_at"]) if row["finished_at"] is not None else None
                ),
            )
            for row in cur.fetchall()
        ]

    @staticmethod
    def _material_version(
        row: dict[str, Any],
        *,
        case_id: str,
        logical_name: str,
    ) -> MaterialVersion:
        return MaterialVersion(
            id=row["id"],
            material_id=row["material_id"],
            case_id=case_id,
            logical_name=logical_name,
            version_number=row["version_number"],
            filename=row["filename"],
            content_type=row["content_type"],
            object_key=row["object_key"],
            sha256=row["sha256"],
            byte_size=row["byte_size"],
            uploaded_by=row["uploaded_by"],
            parse_status=row["parse_status"],
            parser=row["parser"],
            parser_version=row["parser_version"],
            parsed_text=row["parsed_text"],
            created_at=_timestamp(row["created_at"]),
        )

    @staticmethod
    def _review_task(row: dict[str, Any], attempts: list[TaskAttempt]) -> ReviewTask:
        return ReviewTask(
            id=row["id"],
            case_id=row["case_id"],
            material_snapshot_id=row["material_snapshot_id"],
            rule_snapshot_id=row["rule_snapshot_id"],
            idempotency_key=row["idempotency_key"],
            model_id=row["model_id"],
            data_boundary_summary=_json_copy(row["data_boundary_summary_json"] or {}),
            status=row["status"],
            current_node=row["current_node"],
            error_category=row["error_category"],
            error_message=row["error_message"],
            attempt_count=row["attempt_count"],
            result=(_json_copy(row["result_json"]) if row.get("result_json") is not None else None),
            attempts=attempts,
            lease_expires_at=(
                _timestamp(row["lease_expires_at"])
                if row.get("lease_expires_at") is not None
                else None
            ),
            created_at=_timestamp(row["created_at"]),
            updated_at=_timestamp(row["updated_at"]),
        )
