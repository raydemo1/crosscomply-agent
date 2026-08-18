from __future__ import annotations

from collections.abc import Callable
from typing import Any, Self

import pytest

from law_agent.review.enterprise_store import InMemoryEnterpriseStore, PostgresEnterpriseStore


class ScriptedCursor:
    def __init__(self, steps: list[tuple[str, Any]]) -> None:
        self.steps = steps
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []
        self._result: Any = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        expected, self._result = self.steps.pop(0)
        normalized = " ".join(sql.split())
        assert expected in normalized
        self.executed.append((normalized, params))

    def fetchone(self) -> Any:
        return self._result

    def fetchall(self) -> Any:
        return self._result


class ScriptedConnection:
    def __init__(self, steps: list[tuple[str, Any]]) -> None:
        self.cursor_instance = ScriptedCursor(steps)
        self.committed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def cursor(self) -> ScriptedCursor:
        return self.cursor_instance

    def commit(self) -> None:
        self.committed = True


def _factory(connection: ScriptedConnection) -> Callable[[], ScriptedConnection]:
    return lambda: connection


def _task_row(*, status: str = "queued", attempt_count: int = 0) -> dict[str, Any]:
    return {
        "id": "review_task_1",
        "case_id": "case_1",
        "material_snapshot_id": "material_snapshot_1",
        "rule_snapshot_id": "rule_snapshot_1",
        "idempotency_key": "e" * 64,
        "model_id": "approved-model-v1",
        "data_boundary_summary_json": {"deployment": "intranet"},
        "status": status,
        "current_node": None,
        "error_category": None,
        "error_message": None,
        "attempt_count": attempt_count,
        "lease_expires_at": "2026-08-18T02:00:00+00:00" if status == "running" else None,
        "result_json": None,
        "created_at": "2026-08-18T00:00:00+00:00",
        "updated_at": "2026-08-18T00:00:00+00:00",
    }


def test_claim_uses_skip_locked_and_persists_worker_attempt() -> None:
    running = _task_row(status="running", attempt_count=1)
    attempt = {
        "attempt_number": 1,
        "worker_id": "worker-a",
        "status": "running",
        "failed_node": None,
        "error_category": None,
        "error_message": None,
        "started_at": "2026-08-18T00:00:00+00:00",
        "finished_at": None,
    }
    connection = ScriptedConnection(
        [
            ("WITH expired AS", []),
            ("FOR UPDATE SKIP LOCKED", running),
            ("INSERT INTO review_task_attempts", None),
            ("FROM review_task_attempts", [attempt]),
        ]
    )
    store = PostgresEnterpriseStore("postgresql://unused", connect=_factory(connection))

    task = store.claim_next_task(worker_id="worker-a")

    assert task is not None
    assert task.status == "running"
    assert task.attempt_count == 1
    assert task.attempts[-1].worker_id == "worker-a"
    insert_params = connection.cursor_instance.executed[2][1]
    assert insert_params is not None
    assert "worker-a" in insert_params
    assert connection.committed
    assert connection.cursor_instance.steps == []


def test_claim_returns_none_without_creating_attempt() -> None:
    connection = ScriptedConnection([("WITH expired AS", []), ("FOR UPDATE SKIP LOCKED", None)])
    store = PostgresEnterpriseStore("postgresql://unused", connect=_factory(connection))

    assert store.claim_next_task(worker_id="worker-a") is None
    assert len(connection.cursor_instance.executed) == 2


def test_retry_only_requeues_failed_task_without_incrementing_attempt() -> None:
    queued = _task_row(status="queued", attempt_count=2)
    connection = ScriptedConnection(
        [
            ("WHERE id = %s AND status = 'failed'", queued),
            ("FROM review_task_attempts", []),
        ]
    )
    store = PostgresEnterpriseStore("postgresql://unused", connect=_factory(connection))

    task = store.retry_task("review_task_1")

    assert task.status == "queued"
    assert task.attempt_count == 2
    assert task.attempts == []


def test_retry_rejects_non_failed_or_unknown_task() -> None:
    connection = ScriptedConnection(
        [
            ("WHERE id = %s AND status = 'failed'", None),
            ("SELECT status FROM review_tasks", {"status": "running"}),
        ]
    )
    store = PostgresEnterpriseStore("postgresql://unused", connect=_factory(connection))

    with pytest.raises(ValueError, match="只有失败的审查任务可以重试"):
        store.retry_task("review_task_1")


def test_fail_task_preserves_node_category_and_attempt_history() -> None:
    failed = _task_row(status="failed", attempt_count=1)
    failed["current_node"] = "evidence_retrieval"
    failed["error_category"] = "dependency_unavailable"
    failed["error_message"] = "Elasticsearch unavailable"
    attempt = {
        "attempt_number": 1,
        "worker_id": "worker-a",
        "status": "failed",
        "failed_node": "evidence_retrieval",
        "error_category": "dependency_unavailable",
        "error_message": "Elasticsearch unavailable",
        "started_at": "2026-08-18T00:00:00+00:00",
        "finished_at": "2026-08-18T00:01:00+00:00",
    }
    connection = ScriptedConnection(
        [
            ("UPDATE review_tasks", failed),
            ("UPDATE review_task_attempts", None),
            ("FROM review_task_attempts", [attempt]),
        ]
    )
    store = PostgresEnterpriseStore("postgresql://unused", connect=_factory(connection))

    task = store.fail_task(
        "review_task_1",
        failed_node="evidence_retrieval",
        error_category="dependency_unavailable",
        error_message="Elasticsearch unavailable",
    )

    assert task.status == "failed"
    assert task.current_node == "evidence_retrieval"
    assert task.attempts[-1].error_category == "dependency_unavailable"


def test_material_version_number_is_allocated_while_material_row_is_locked() -> None:
    version_row = {
        "id": "material_version_1",
        "material_id": "material_1",
        "case_id": "case_1",
        "logical_name": "contract",
        "version_number": 3,
        "filename": "contract-v3.pdf",
        "content_type": "application/pdf",
        "object_key": "cases/case_1/contract/v3.pdf",
        "sha256": "a" * 64,
        "byte_size": 42,
        "uploaded_by": "user_1",
        "parse_status": "pending",
        "parser": None,
        "parser_version": None,
        "parsed_text": None,
        "created_at": "2026-08-18T00:00:00+00:00",
    }
    connection = ScriptedConnection(
        [
            ("INSERT INTO materials", {"id": "material_1"}),
            ("SELECT COALESCE(MAX(version_number), 0) + 1", {"version_number": 3}),
            ("INSERT INTO material_versions", version_row),
        ]
    )
    store = PostgresEnterpriseStore("postgresql://unused", connect=_factory(connection))

    version = store.create_material_version(
        case_id="case_1",
        logical_name="contract",
        filename="contract-v3.pdf",
        content_type="application/pdf",
        object_key="cases/case_1/contract/v3.pdf",
        sha256="a" * 64,
        byte_size=42,
        uploaded_by="user_1",
    )

    assert version.version_number == 3
    first_sql = connection.cursor_instance.executed[0][0]
    assert "ON CONFLICT (case_id, logical_name) DO UPDATE" in first_sql
    assert connection.committed


def test_complete_task_saves_result_and_finishes_current_attempt() -> None:
    succeeded = _task_row(status="succeeded", attempt_count=1)
    succeeded["current_node"] = "decision_report"
    succeeded["result_json"] = {"decision": "conditional"}
    attempt = {
        "attempt_number": 1,
        "worker_id": "worker-a",
        "status": "succeeded",
        "failed_node": None,
        "error_category": None,
        "error_message": None,
        "started_at": "2026-08-18T00:00:00+00:00",
        "finished_at": "2026-08-18T00:01:00+00:00",
    }
    connection = ScriptedConnection(
        [
            ("UPDATE review_tasks", succeeded),
            ("UPDATE review_task_attempts", None),
            ("FROM review_task_attempts", [attempt]),
        ]
    )
    store = PostgresEnterpriseStore("postgresql://unused", connect=_factory(connection))

    task = store.complete_task(
        "review_task_1",
        result={"decision": "conditional"},
        final_node="decision_report",
    )

    assert task.status == "succeeded"
    assert task.result == {"decision": "conditional"}
    assert task.current_node == "decision_report"
    assert task.attempts[-1].status == "succeeded"


def test_in_memory_complete_task_matches_persistent_interface() -> None:
    store = InMemoryEnterpriseStore()
    version = store.create_material_version(
        case_id="case_1",
        logical_name="contract",
        filename="contract.pdf",
        content_type="application/pdf",
        object_key="contract.pdf",
        sha256="f" * 64,
        byte_size=10,
        uploaded_by="user_1",
    )
    snapshot = store.create_material_snapshot(
        case_id="case_1", version_ids=[version.id], created_by="user_1"
    )
    rule = store.create_rule_snapshot(
        case_id="case_1",
        material_snapshot_id=snapshot.id,
        ruleset_version="v1",
        facts={},
        determination={},
    )
    task = store.enqueue_review_task(
        case_id="case_1",
        material_snapshot_id=snapshot.id,
        rule_snapshot_id=rule.id,
        model_id="model-v1",
        data_boundary_summary={},
    )
    store.claim_next_task(worker_id="worker-a")

    completed = store.complete_task(
        task.id,
        result={"decision": "approved"},
        final_node="decision_report",
    )

    assert store.get_task(task.id) is completed
    assert completed.status == "succeeded"
    assert completed.attempts[-1].status == "succeeded"
    assert completed.attempts[-1].finished_at is not None
