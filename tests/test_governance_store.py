from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Self

import pytest

from law_agent.review.governance_store import (
    InMemoryGovernanceStore,
    PostgresGovernanceStore,
)


def _approval(store: InMemoryGovernanceStore):
    return store.create_approval_record(
        case_id="case_1",
        task_id="task_1",
        instance_id="feishu_instance_1",
        payload={"request_id": "request_1"},
    )


def test_approval_instance_creation_is_idempotent() -> None:
    store = InMemoryGovernanceStore()

    first = _approval(store)
    repeated = _approval(store)

    assert repeated is first
    assert store.get_approval_by_instance("feishu_instance_1") is first
    with pytest.raises(ValueError, match="审批实例 ID 已绑定其他记录"):
        store.create_approval_record(
            case_id="case_2",
            task_id="task_2",
            instance_id="feishu_instance_1",
        )


def test_event_is_idempotent_and_persists_signature_and_payload_hash() -> None:
    store = InMemoryGovernanceStore()
    approval = _approval(store)
    payload = {"event": {"status": "APPROVED"}}

    first = store.record_approval_event(
        approval_id=approval.id,
        provider_event_id="event_1",
        event_type="approval_instance",
        signature_valid=True,
        payload=payload,
        target_status="approved",
        approver_name="张法务",
        decided_at="2026-08-18T09:30:00+08:00",
    )
    repeated = store.record_approval_event(
        approval_id=approval.id,
        provider_event_id="event_1",
        event_type="approval_instance",
        signature_valid=True,
        payload=payload,
        target_status="approved",
        approver_name="张法务",
        decided_at="2026-08-18T09:30:00+08:00",
    )

    assert not first.duplicate
    assert repeated.duplicate
    assert repeated.event.payload_sha256 == first.event.payload_sha256
    assert repeated.event.signature_valid
    assert repeated.approval.status == "approved"


def test_invalid_signature_is_audited_without_changing_decision() -> None:
    store = InMemoryGovernanceStore()
    approval = _approval(store)

    receipt = store.record_approval_event(
        approval_id=approval.id,
        provider_event_id="event_invalid",
        event_type="approval_instance",
        signature_valid=False,
        payload={"event": {"status": "REJECTED"}},
        target_status="rejected",
    )

    assert not receipt.event.signature_valid
    assert receipt.approval.status == "pending"


def test_feishu_terminal_decision_cannot_be_overwritten() -> None:
    store = InMemoryGovernanceStore()
    approval = _approval(store)
    store.record_approval_event(
        approval_id=approval.id,
        provider_event_id="event_approved",
        event_type="approval_instance",
        signature_valid=True,
        payload={"status": "APPROVED"},
        target_status="approved",
    )

    with pytest.raises(ValueError, match="审批终态不可覆盖"):
        store.record_approval_event(
            approval_id=approval.id,
            provider_event_id="event_rejected",
            event_type="approval_instance",
            signature_valid=True,
            payload={"status": "REJECTED"},
            target_status="rejected",
        )


def test_duplicate_event_id_with_different_payload_is_rejected() -> None:
    store = InMemoryGovernanceStore()
    approval = _approval(store)
    store.record_approval_event(
        approval_id=approval.id,
        provider_event_id="event_1",
        event_type="approval_instance",
        signature_valid=True,
        payload={"status": "APPROVED"},
        target_status="approved",
    )

    with pytest.raises(ValueError, match="事件 ID 冲突"):
        store.record_approval_event(
            approval_id=approval.id,
            provider_event_id="event_1",
            event_type="approval_instance",
            signature_valid=True,
            payload={"status": "REJECTED"},
            target_status="approved",
        )


def test_report_records_are_immutable_and_latest_is_returned() -> None:
    store = InMemoryGovernanceStore()
    approval = _approval(store)

    first = store.create_report_record(
        case_id="case_1",
        approval_id=approval.id,
        object_key="cases/case_1/reports/v1.pdf",
        sha256="a" * 64,
        metadata={"version": 1},
    )
    second = store.create_report_record(
        case_id="case_1",
        approval_id=approval.id,
        object_key="cases/case_1/reports/v2.pdf",
        sha256="b" * 64,
        metadata={"version": 2},
    )

    assert store.get_report(first.id) is first
    assert store.get_latest_report("case_1") is second
    assert store.list_reports("case_1") == [first, second]


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


def _approval_row(status: str = "pending") -> dict[str, Any]:
    return {
        "id": "approval_1",
        "case_id": "case_1",
        "task_id": "task_1",
        "provider": "feishu",
        "instance_id": "instance_1",
        "status": status,
        "approver_name": None,
        "decided_at": None,
        "payload_json": {},
        "created_at": "2026-08-18T00:00:00+00:00",
        "updated_at": "2026-08-18T00:00:00+00:00",
    }


def _event_row() -> dict[str, Any]:
    payload = {"status": "APPROVED"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return {
        "id": "approval_event_1",
        "approval_id": "approval_1",
        "provider_event_id": "event_1",
        "event_type": "approval_instance",
        "signature_valid": True,
        "payload_sha256": hashlib.sha256(encoded).hexdigest(),
        "payload_json": payload,
        "created_at": "2026-08-18T00:00:00+00:00",
    }


def test_postgres_event_checks_duplicate_before_locking_and_mutating() -> None:
    event = _event_row()
    approval = _approval_row("approved")
    connection = ScriptedConnection(
        [
            ("FROM approval_events", event),
            ("FROM approval_records", approval),
        ]
    )
    store = PostgresGovernanceStore("postgresql://unused", connect=_factory(connection))

    receipt = store.record_approval_event(
        approval_id="approval_1",
        provider_event_id="event_1",
        event_type="approval_instance",
        signature_valid=True,
        payload={"status": "APPROVED"},
        target_status="approved",
    )

    assert receipt.duplicate
    assert receipt.approval.status == "approved"
    assert connection.cursor_instance.steps == []


def test_postgres_new_terminal_event_locks_record_and_updates_atomically() -> None:
    pending = _approval_row()
    approved = _approval_row("approved")
    approved["approver_name"] = "张法务"
    approved["decided_at"] = "2026-08-18T09:30:00+08:00"
    event = _event_row()
    connection = ScriptedConnection(
        [
            ("FROM approval_events", None),
            ("FOR UPDATE", pending),
            ("FROM approval_events", None),
            ("INSERT INTO approval_events", event),
            ("UPDATE approval_records", approved),
        ]
    )
    store = PostgresGovernanceStore("postgresql://unused", connect=_factory(connection))

    receipt = store.record_approval_event(
        approval_id="approval_1",
        provider_event_id="event_1",
        event_type="approval_instance",
        signature_valid=True,
        payload={"status": "APPROVED"},
        target_status="approved",
        approver_name="张法务",
        decided_at="2026-08-18T09:30:00+08:00",
    )

    assert receipt.approval.status == "approved"
    assert connection.committed
    assert connection.cursor_instance.steps == []


def test_approval_delivery_failure_is_preserved_across_manual_retry() -> None:
    store = InMemoryGovernanceStore()
    queued = store.enqueue_approval_delivery(
        case_id="case_1",
        task_id="task_1",
        idempotency_key="approval-request-1",
    )

    first_attempt = store.begin_approval_attempt(queued.id)
    failed = store.fail_approval_delivery(
        queued.id,
        error_message="Feishu gateway timed out",
    )

    assert first_attempt.attempt_count == 1
    assert failed.status == "failed"
    assert failed.error_message == "Feishu gateway timed out"
    requeued = store.retry_approval_delivery(queued.id)
    assert requeued.status == "queued"
    assert requeued.attempt_count == 1
    assert requeued.error_message == "Feishu gateway timed out"

    second_attempt = store.begin_approval_attempt(queued.id)
    assert second_attempt.attempt_count == 2
    assert second_attempt.error_message == "Feishu gateway timed out"


def test_approval_delivery_enqueue_is_idempotent_and_success_cannot_retry() -> None:
    store = InMemoryGovernanceStore()
    first = store.enqueue_approval_delivery(
        case_id="case_1",
        task_id="task_1",
        idempotency_key="approval-request-1",
    )
    repeated = store.enqueue_approval_delivery(
        case_id="case_1",
        task_id="task_1",
        idempotency_key="approval-request-1",
    )
    store.begin_approval_attempt(first.id)
    succeeded = store.succeed_approval_delivery(first.id, instance_id="instance_1")

    assert repeated is first
    assert store.get_approval_delivery(first.id) is succeeded
    assert succeeded.status == "succeeded"
    assert succeeded.instance_id == "instance_1"
    assert succeeded.error_message is None
    with pytest.raises(ValueError, match="只有失败的审批投递可以重试"):
        store.retry_approval_delivery(first.id)


def _delivery_row(
    *,
    status: str,
    attempt_count: int,
    error_message: str | None = None,
    instance_id: str | None = None,
    lease_expires_at: str | None = None,
) -> dict[str, Any]:
    return {
        "id": "approval_delivery_1",
        "case_id": "case_1",
        "task_id": "task_1",
        "idempotency_key": "approval-request-1",
        "status": status,
        "attempt_count": attempt_count,
        "instance_id": instance_id,
        "error_message": error_message,
        "lease_expires_at": lease_expires_at,
        "created_at": "2026-08-18T00:00:00+00:00",
        "updated_at": "2026-08-18T00:00:00+00:00",
    }


def test_postgres_delivery_retry_increments_only_when_attempt_begins() -> None:
    running_1 = _delivery_row(status="running", attempt_count=1)
    failed = _delivery_row(
        status="failed",
        attempt_count=1,
        error_message="Feishu gateway timed out",
    )
    queued = _delivery_row(
        status="queued",
        attempt_count=1,
        error_message="Feishu gateway timed out",
    )
    running_2 = _delivery_row(
        status="running",
        attempt_count=2,
        error_message="Feishu gateway timed out",
    )
    connection = ScriptedConnection(
        [
            ("status = 'running'", running_1),
            ("status = 'failed'", failed),
            ("status = 'queued'", queued),
            ("status = 'running'", running_2),
        ]
    )
    store = PostgresGovernanceStore("postgresql://unused", connect=_factory(connection))

    assert store.begin_approval_attempt("approval_delivery_1").attempt_count == 1
    failed_result = store.fail_approval_delivery(
        "approval_delivery_1",
        error_message="Feishu gateway timed out",
    )
    assert failed_result.error_message == "Feishu gateway timed out"
    assert store.retry_approval_delivery("approval_delivery_1").attempt_count == 1
    second_attempt = store.begin_approval_attempt("approval_delivery_1")
    assert second_attempt.attempt_count == 2
    assert second_attempt.error_message == "Feishu gateway timed out"
    assert connection.cursor_instance.steps == []


def test_expired_delivery_lease_is_failed_and_can_be_retried() -> None:
    current = [datetime(2026, 8, 18, 0, 0, tzinfo=UTC)]
    store = InMemoryGovernanceStore(clock=lambda: current[0])
    delivery = store.enqueue_approval_delivery(
        case_id="case_1",
        task_id="task_1",
        idempotency_key="approval-request-1",
    )
    running = store.begin_approval_attempt(delivery.id, lease_seconds=30)
    assert running.lease_expires_at == "2026-08-18T00:00:30+00:00"

    current[0] += timedelta(seconds=31)
    recovered = store.requeue_expired_approval_deliveries()

    assert [item.id for item in recovered] == [delivery.id]
    assert recovered[0].status == "failed"
    assert recovered[0].error_message == "审批投递租约已过期"
    assert recovered[0].lease_expires_at is None
    assert store.retry_approval_delivery(delivery.id).status == "queued"


def test_unexpired_lease_is_not_recovered_and_queued_recovery_retries() -> None:
    current = [datetime(2026, 8, 18, 0, 0, tzinfo=UTC)]
    store = InMemoryGovernanceStore(clock=lambda: current[0])
    delivery = store.enqueue_approval_delivery(
        case_id="case_1",
        task_id="task_1",
        idempotency_key="approval-request-1",
    )
    store.begin_approval_attempt(delivery.id, lease_seconds=30)

    assert store.requeue_expired_approval_deliveries(target_status="queued") == []
    current[0] += timedelta(seconds=31)
    recovered = store.requeue_expired_approval_deliveries(target_status="queued")

    assert recovered[0].status == "queued"
    second_attempt = store.begin_approval_attempt(delivery.id, lease_seconds=30)
    assert second_attempt.attempt_count == 2


def test_expired_lease_cannot_report_late_success() -> None:
    current = [datetime(2026, 8, 18, 0, 0, tzinfo=UTC)]
    store = InMemoryGovernanceStore(clock=lambda: current[0])
    delivery = store.enqueue_approval_delivery(
        case_id="case_1",
        task_id="task_1",
        idempotency_key="approval-request-1",
    )
    store.begin_approval_attempt(delivery.id, lease_seconds=30)
    current[0] += timedelta(seconds=31)

    with pytest.raises(ValueError, match="审批投递租约已过期"):
        store.succeed_approval_delivery(delivery.id, instance_id="late_instance")


def test_postgres_expired_lease_recovery_is_persistent() -> None:
    recovered = _delivery_row(
        status="failed",
        attempt_count=1,
        error_message="审批投递租约已过期",
    )
    connection = ScriptedConnection([("lease_expires_at <= COALESCE", [recovered])])
    store = PostgresGovernanceStore("postgresql://unused", connect=_factory(connection))

    result = store.requeue_expired_approval_deliveries(
        now="2026-08-18T00:01:00+00:00",
    )

    assert result[0].status == "failed"
    assert result[0].lease_expires_at is None
    assert connection.committed
