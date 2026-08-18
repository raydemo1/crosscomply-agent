"""Durable approval-event audit and immutable decision-report records."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from law_agent.review.case_store import utc_now

ApprovalStatus = Literal[
    "pending",
    "approved",
    "conditionally_approved",
    "rejected",
    "withdrawn",
]
TerminalApprovalStatus = Literal[
    "approved",
    "conditionally_approved",
    "rejected",
    "withdrawn",
]
ApprovalDeliveryStatus = Literal["queued", "running", "failed", "succeeded"]

_TERMINAL_STATUSES = frozenset({"approved", "conditionally_approved", "rejected", "withdrawn"})


def _identifier(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


def _json_copy(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False))


def _payload_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _timestamp(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


@dataclass(frozen=True)
class ApprovalRecord:
    id: str
    case_id: str
    task_id: str
    provider: str
    instance_id: str
    status: ApprovalStatus = "pending"
    approver_name: str | None = None
    decided_at: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class ApprovalEvent:
    id: str
    approval_id: str
    provider_event_id: str
    event_type: str
    signature_valid: bool
    payload_sha256: str
    payload: dict[str, Any]
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class ApprovalEventReceipt:
    event: ApprovalEvent
    approval: ApprovalRecord
    duplicate: bool


@dataclass(frozen=True)
class ReportRecord:
    id: str
    case_id: str
    approval_id: str
    object_key: str
    sha256: str
    metadata: dict[str, Any]
    created_at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class ApprovalDelivery:
    id: str
    case_id: str
    task_id: str
    idempotency_key: str
    status: ApprovalDeliveryStatus = "queued"
    attempt_count: int = 0
    instance_id: str | None = None
    error_message: str | None = None
    lease_expires_at: str | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


def _validate_transition(current: ApprovalStatus, target: TerminalApprovalStatus) -> None:
    if current in _TERMINAL_STATUSES and current != target:
        raise ValueError(f"飞书审批终态不可覆盖：{current} -> {target}")
    if current != "pending" and current != target:
        raise ValueError(f"审批状态不能从 {current} 更新为 {target}")


def _validate_digest(sha256: str) -> None:
    if len(sha256) != 64 or any(char not in "0123456789abcdefABCDEF" for char in sha256):
        raise ValueError("报告 SHA-256 必须是 64 位十六进制字符串")


class InMemoryGovernanceStore:
    """Thread-safe test double matching the PostgreSQL governance interface."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._lock = threading.RLock()
        self._clock = clock or (lambda: datetime.now(UTC))
        self.approvals: dict[str, ApprovalRecord] = {}
        self.approval_by_instance: dict[str, str] = {}
        self.events: dict[str, ApprovalEvent] = {}
        self.event_by_provider_id: dict[str, str] = {}
        self.reports: dict[str, ReportRecord] = {}
        self.report_by_object_key: dict[str, str] = {}
        self.approval_deliveries: dict[str, ApprovalDelivery] = {}
        self.delivery_by_key: dict[str, str] = {}

    def enqueue_approval_delivery(
        self,
        *,
        case_id: str,
        task_id: str,
        idempotency_key: str,
    ) -> ApprovalDelivery:
        if not idempotency_key:
            raise ValueError("审批投递幂等键不能为空")
        with self._lock:
            existing_id = self.delivery_by_key.get(idempotency_key)
            if existing_id is not None:
                existing = self.approval_deliveries[existing_id]
                if existing.case_id != case_id or existing.task_id != task_id:
                    raise ValueError("审批投递幂等键已绑定其他任务")
                return existing
            delivery = ApprovalDelivery(
                id=_identifier("approval_delivery"),
                case_id=case_id,
                task_id=task_id,
                idempotency_key=idempotency_key,
            )
            self.approval_deliveries[delivery.id] = delivery
            self.delivery_by_key[idempotency_key] = delivery.id
            return delivery

    def get_approval_delivery(self, delivery_id: str) -> ApprovalDelivery | None:
        with self._lock:
            return self.approval_deliveries.get(delivery_id)

    def begin_approval_attempt(
        self,
        delivery_id: str,
        *,
        lease_seconds: int = 60,
    ) -> ApprovalDelivery:
        if lease_seconds <= 0:
            raise ValueError("审批投递租约必须大于 0 秒")
        with self._lock:
            delivery = self._delivery_or_raise(delivery_id)
            if delivery.status != "queued":
                raise ValueError("只有排队中的审批投递可以开始尝试")
            now = self._clock()
            updated = replace(
                delivery,
                status="running",
                attempt_count=delivery.attempt_count + 1,
                lease_expires_at=(now + timedelta(seconds=lease_seconds)).isoformat(),
                updated_at=now.isoformat(),
            )
            self.approval_deliveries[delivery_id] = updated
            return updated

    def fail_approval_delivery(
        self,
        delivery_id: str,
        *,
        error_message: str,
    ) -> ApprovalDelivery:
        if not error_message:
            raise ValueError("审批投递失败信息不能为空")
        with self._lock:
            delivery = self._delivery_or_raise(delivery_id)
            if delivery.status != "running":
                raise ValueError("只有运行中的审批投递可以标记失败")
            updated = replace(
                delivery,
                status="failed",
                error_message=error_message,
                lease_expires_at=None,
                updated_at=utc_now(),
            )
            self.approval_deliveries[delivery_id] = updated
            return updated

    def succeed_approval_delivery(
        self,
        delivery_id: str,
        *,
        instance_id: str,
    ) -> ApprovalDelivery:
        if not instance_id:
            raise ValueError("飞书审批实例 ID 不能为空")
        with self._lock:
            delivery = self._delivery_or_raise(delivery_id)
            if delivery.status != "running":
                raise ValueError("只有运行中的审批投递可以标记成功")
            if (
                delivery.lease_expires_at is None
                or datetime.fromisoformat(delivery.lease_expires_at) <= self._clock()
            ):
                raise ValueError("审批投递租约已过期，不能接受迟到的成功结果")
            updated = replace(
                delivery,
                status="succeeded",
                instance_id=instance_id,
                error_message=None,
                lease_expires_at=None,
                updated_at=utc_now(),
            )
            self.approval_deliveries[delivery_id] = updated
            return updated

    def retry_approval_delivery(self, delivery_id: str) -> ApprovalDelivery:
        with self._lock:
            delivery = self._delivery_or_raise(delivery_id)
            if delivery.status != "failed":
                raise ValueError("只有失败的审批投递可以重试")
            updated = replace(
                delivery,
                status="queued",
                lease_expires_at=None,
                updated_at=utc_now(),
            )
            self.approval_deliveries[delivery_id] = updated
            return updated

    def requeue_expired_approval_deliveries(
        self,
        *,
        now: str | None = None,
        target_status: Literal["failed", "queued"] = "failed",
    ) -> list[ApprovalDelivery]:
        if target_status not in {"failed", "queued"}:
            raise ValueError("过期审批投递只能回收到 failed 或 queued")
        cutoff = datetime.fromisoformat(now) if now is not None else self._clock()
        recovered: list[ApprovalDelivery] = []
        with self._lock:
            for delivery_id, delivery in self.approval_deliveries.items():
                if delivery.status != "running" or delivery.lease_expires_at is None:
                    continue
                if datetime.fromisoformat(delivery.lease_expires_at) > cutoff:
                    continue
                updated = replace(
                    delivery,
                    status=target_status,
                    error_message="审批投递租约已过期",
                    lease_expires_at=None,
                    updated_at=cutoff.isoformat(),
                )
                self.approval_deliveries[delivery_id] = updated
                recovered.append(updated)
        return recovered

    def _delivery_or_raise(self, delivery_id: str) -> ApprovalDelivery:
        delivery = self.approval_deliveries.get(delivery_id)
        if delivery is None:
            raise KeyError(delivery_id)
        return delivery

    def create_approval_record(
        self,
        *,
        case_id: str,
        task_id: str,
        instance_id: str,
        payload: dict[str, Any] | None = None,
        provider: str = "feishu",
    ) -> ApprovalRecord:
        normalized_payload = _json_copy(payload or {})
        with self._lock:
            existing_id = self.approval_by_instance.get(instance_id)
            if existing_id is not None:
                existing = self.approvals[existing_id]
                if (
                    existing.case_id != case_id
                    or existing.task_id != task_id
                    or existing.provider != provider
                    or existing.payload != normalized_payload
                ):
                    raise ValueError("审批实例 ID 已绑定其他记录")
                return existing
            record = ApprovalRecord(
                id=_identifier("approval"),
                case_id=case_id,
                task_id=task_id,
                provider=provider,
                instance_id=instance_id,
                payload=normalized_payload,
            )
            self.approvals[record.id] = record
            self.approval_by_instance[instance_id] = record.id
            return record

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        with self._lock:
            return self.approvals.get(approval_id)

    def get_approval_by_instance(self, instance_id: str) -> ApprovalRecord | None:
        with self._lock:
            approval_id = self.approval_by_instance.get(instance_id)
            return self.approvals.get(approval_id) if approval_id else None

    def get_latest_approval(self, case_id: str) -> ApprovalRecord | None:
        with self._lock:
            candidates = [item for item in self.approvals.values() if item.case_id == case_id]
            return candidates[-1] if candidates else None

    def record_approval_event(
        self,
        *,
        approval_id: str,
        provider_event_id: str,
        event_type: str,
        signature_valid: bool,
        payload: dict[str, Any],
        target_status: TerminalApprovalStatus | None = None,
        approver_name: str | None = None,
        decided_at: str | None = None,
    ) -> ApprovalEventReceipt:
        normalized_payload = _json_copy(payload)
        digest = _payload_hash(normalized_payload)
        with self._lock:
            existing_event_id = self.event_by_provider_id.get(provider_event_id)
            if existing_event_id is not None:
                event = self.events[existing_event_id]
                self._check_duplicate_event(
                    event,
                    approval_id=approval_id,
                    event_type=event_type,
                    signature_valid=signature_valid,
                    payload_sha256=digest,
                )
                return ApprovalEventReceipt(
                    event=event,
                    approval=self.approvals[event.approval_id],
                    duplicate=True,
                )
            approval = self.approvals.get(approval_id)
            if approval is None:
                raise KeyError(approval_id)
            if signature_valid and target_status is not None:
                _validate_transition(approval.status, target_status)
            event = ApprovalEvent(
                id=_identifier("approval_event"),
                approval_id=approval_id,
                provider_event_id=provider_event_id,
                event_type=event_type,
                signature_valid=signature_valid,
                payload_sha256=digest,
                payload=normalized_payload,
            )
            self.events[event.id] = event
            self.event_by_provider_id[provider_event_id] = event.id
            if signature_valid and target_status is not None and approval.status != target_status:
                approval = ApprovalRecord(
                    id=approval.id,
                    case_id=approval.case_id,
                    task_id=approval.task_id,
                    provider=approval.provider,
                    instance_id=approval.instance_id,
                    status=target_status,
                    approver_name=approver_name,
                    decided_at=decided_at,
                    payload=approval.payload,
                    created_at=approval.created_at,
                    updated_at=utc_now(),
                )
                self.approvals[approval.id] = approval
            return ApprovalEventReceipt(event=event, approval=approval, duplicate=False)

    def create_report_record(
        self,
        *,
        case_id: str,
        approval_id: str,
        object_key: str,
        sha256: str,
        metadata: dict[str, Any] | None = None,
    ) -> ReportRecord:
        _validate_digest(sha256)
        normalized_metadata = _json_copy(metadata or {})
        with self._lock:
            approval = self.approvals.get(approval_id)
            if approval is None or approval.case_id != case_id:
                raise ValueError("报告必须绑定本案件的审批记录")
            existing_id = self.report_by_object_key.get(object_key)
            if existing_id is not None:
                existing = self.reports[existing_id]
                if (
                    existing.case_id != case_id
                    or existing.approval_id != approval_id
                    or existing.sha256 != sha256.lower()
                    or existing.metadata != normalized_metadata
                ):
                    raise ValueError("报告对象键已绑定其他记录")
                return existing
            record = ReportRecord(
                id=_identifier("report"),
                case_id=case_id,
                approval_id=approval_id,
                object_key=object_key,
                sha256=sha256.lower(),
                metadata=normalized_metadata,
            )
            self.reports[record.id] = record
            self.report_by_object_key[object_key] = record.id
            return record

    def get_report(self, report_id: str) -> ReportRecord | None:
        with self._lock:
            return self.reports.get(report_id)

    def list_reports(self, case_id: str) -> list[ReportRecord]:
        with self._lock:
            return [item for item in self.reports.values() if item.case_id == case_id]

    def get_latest_report(self, case_id: str) -> ReportRecord | None:
        reports = self.list_reports(case_id)
        return reports[-1] if reports else None

    @staticmethod
    def _check_duplicate_event(
        event: ApprovalEvent,
        *,
        approval_id: str,
        event_type: str,
        signature_valid: bool,
        payload_sha256: str,
    ) -> None:
        if (
            event.approval_id != approval_id
            or event.event_type != event_type
            or event.signature_valid != signature_valid
            or event.payload_sha256 != payload_sha256
        ):
            raise ValueError("飞书 provider_event_id 事件 ID 冲突")


class PostgresGovernanceStore:
    """PostgreSQL approval audit and report metadata store."""

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

    def enqueue_approval_delivery(
        self,
        *,
        case_id: str,
        task_id: str,
        idempotency_key: str,
    ) -> ApprovalDelivery:
        if not idempotency_key:
            raise ValueError("审批投递幂等键不能为空")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO approval_delivery_jobs (
                    id, case_id, task_id, idempotency_key
                ) VALUES (%s, %s, %s, %s)
                ON CONFLICT (idempotency_key) DO UPDATE
                SET idempotency_key = EXCLUDED.idempotency_key
                RETURNING *
                """,
                (
                    _identifier("approval_delivery"),
                    case_id,
                    task_id,
                    idempotency_key,
                ),
            )
            row = cur.fetchone()
            assert row is not None
            delivery = self._delivery(row)
            if delivery.case_id != case_id or delivery.task_id != task_id:
                raise ValueError("审批投递幂等键已绑定其他任务")
            conn.commit()
        return delivery

    def get_approval_delivery(self, delivery_id: str) -> ApprovalDelivery | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM approval_delivery_jobs WHERE id = %s", (delivery_id,))
            row = cur.fetchone()
        return self._delivery(row) if row is not None else None

    def begin_approval_attempt(
        self,
        delivery_id: str,
        *,
        lease_seconds: int = 60,
    ) -> ApprovalDelivery:
        if lease_seconds <= 0:
            raise ValueError("审批投递租约必须大于 0 秒")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE approval_delivery_jobs
                SET status = 'running', attempt_count = attempt_count + 1,
                    lease_expires_at = now() + (%s * interval '1 second'),
                    updated_at = now()
                WHERE id = %s AND status = 'queued'
                RETURNING *
                """,
                (lease_seconds, delivery_id),
            )
            row = cur.fetchone()
            if row is None:
                self._raise_delivery_state(
                    cur,
                    delivery_id,
                    "只有排队中的审批投递可以开始尝试",
                )
            assert row is not None
            conn.commit()
        return self._delivery(row)

    def fail_approval_delivery(
        self,
        delivery_id: str,
        *,
        error_message: str,
    ) -> ApprovalDelivery:
        if not error_message:
            raise ValueError("审批投递失败信息不能为空")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE approval_delivery_jobs
                SET status = 'failed', error_message = %s,
                    lease_expires_at = NULL, updated_at = now()
                WHERE id = %s AND status = 'running'
                RETURNING *
                """,
                (error_message, delivery_id),
            )
            row = cur.fetchone()
            if row is None:
                self._raise_delivery_state(
                    cur,
                    delivery_id,
                    "只有运行中的审批投递可以标记失败",
                )
            assert row is not None
            conn.commit()
        return self._delivery(row)

    def succeed_approval_delivery(
        self,
        delivery_id: str,
        *,
        instance_id: str,
    ) -> ApprovalDelivery:
        if not instance_id:
            raise ValueError("飞书审批实例 ID 不能为空")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE approval_delivery_jobs
                SET status = 'succeeded', instance_id = %s,
                    error_message = NULL, lease_expires_at = NULL, updated_at = now()
                WHERE id = %s AND status = 'running' AND lease_expires_at > now()
                RETURNING *
                """,
                (instance_id, delivery_id),
            )
            row = cur.fetchone()
            if row is None:
                self._raise_delivery_state(
                    cur,
                    delivery_id,
                    "只有租约有效的运行中审批投递可以标记成功",
                )
            assert row is not None
            conn.commit()
        return self._delivery(row)

    def retry_approval_delivery(self, delivery_id: str) -> ApprovalDelivery:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE approval_delivery_jobs
                SET status = 'queued', lease_expires_at = NULL, updated_at = now()
                WHERE id = %s AND status = 'failed'
                RETURNING *
                """,
                (delivery_id,),
            )
            row = cur.fetchone()
            if row is None:
                self._raise_delivery_state(
                    cur,
                    delivery_id,
                    "只有失败的审批投递可以重试",
                )
            assert row is not None
            conn.commit()
        return self._delivery(row)

    def requeue_expired_approval_deliveries(
        self,
        *,
        now: str | None = None,
        target_status: Literal["failed", "queued"] = "failed",
    ) -> list[ApprovalDelivery]:
        if target_status not in {"failed", "queued"}:
            raise ValueError("过期审批投递只能回收到 failed 或 queued")
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE approval_delivery_jobs
                SET status = %s, error_message = '审批投递租约已过期',
                    lease_expires_at = NULL, updated_at = now()
                WHERE status = 'running'
                  AND lease_expires_at <= COALESCE(%s::timestamptz, now())
                RETURNING *
                """,
                (target_status, now),
            )
            rows = cur.fetchall()
            conn.commit()
        return [self._delivery(row) for row in rows]

    @staticmethod
    def _raise_delivery_state(
        cur: Any,
        delivery_id: str,
        message: str,
    ) -> None:
        cur.execute("SELECT status FROM approval_delivery_jobs WHERE id = %s", (delivery_id,))
        if cur.fetchone() is None:
            raise KeyError(delivery_id)
        raise ValueError(message)

    def create_approval_record(
        self,
        *,
        case_id: str,
        task_id: str,
        instance_id: str,
        payload: dict[str, Any] | None = None,
        provider: str = "feishu",
    ) -> ApprovalRecord:
        normalized_payload = payload or {}
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO approval_records (
                    id, case_id, task_id, provider, instance_id, payload_json
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (instance_id) DO UPDATE
                SET instance_id = EXCLUDED.instance_id
                RETURNING *
                """,
                (
                    _identifier("approval"),
                    case_id,
                    task_id,
                    provider,
                    instance_id,
                    Jsonb(normalized_payload),
                ),
            )
            row = cur.fetchone()
            assert row is not None
            record = self._approval(row)
            if (
                record.case_id != case_id
                or record.task_id != task_id
                or record.provider != provider
                or record.payload != normalized_payload
            ):
                raise ValueError("审批实例 ID 已绑定其他记录")
            conn.commit()
        return record

    def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        return self._select_approval("id", approval_id)

    def get_approval_by_instance(self, instance_id: str) -> ApprovalRecord | None:
        return self._select_approval("instance_id", instance_id)

    def get_latest_approval(self, case_id: str) -> ApprovalRecord | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM approval_records
                WHERE case_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (case_id,),
            )
            row = cur.fetchone()
        return self._approval(row) if row is not None else None

    def record_approval_event(
        self,
        *,
        approval_id: str,
        provider_event_id: str,
        event_type: str,
        signature_valid: bool,
        payload: dict[str, Any],
        target_status: TerminalApprovalStatus | None = None,
        approver_name: str | None = None,
        decided_at: str | None = None,
    ) -> ApprovalEventReceipt:
        digest = _payload_hash(payload)
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM approval_events WHERE provider_event_id = %s",
                (provider_event_id,),
            )
            existing_row = cur.fetchone()
            if existing_row is not None:
                event = self._event(existing_row)
                InMemoryGovernanceStore._check_duplicate_event(
                    event,
                    approval_id=approval_id,
                    event_type=event_type,
                    signature_valid=signature_valid,
                    payload_sha256=digest,
                )
                cur.execute("SELECT * FROM approval_records WHERE id = %s", (event.approval_id,))
                approval_row = cur.fetchone()
                assert approval_row is not None
                return ApprovalEventReceipt(
                    event=event,
                    approval=self._approval(approval_row),
                    duplicate=True,
                )
            cur.execute(
                "SELECT * FROM approval_records WHERE id = %s FOR UPDATE",
                (approval_id,),
            )
            approval_row = cur.fetchone()
            if approval_row is None:
                raise KeyError(approval_id)
            approval = self._approval(approval_row)
            # A concurrent delivery may have inserted the same provider event
            # while this transaction waited for the approval-row lock.
            cur.execute(
                "SELECT * FROM approval_events WHERE provider_event_id = %s",
                (provider_event_id,),
            )
            concurrent_row = cur.fetchone()
            if concurrent_row is not None:
                event = self._event(concurrent_row)
                InMemoryGovernanceStore._check_duplicate_event(
                    event,
                    approval_id=approval_id,
                    event_type=event_type,
                    signature_valid=signature_valid,
                    payload_sha256=digest,
                )
                return ApprovalEventReceipt(
                    event=event,
                    approval=approval,
                    duplicate=True,
                )
            if signature_valid and target_status is not None:
                _validate_transition(approval.status, target_status)
            cur.execute(
                """
                INSERT INTO approval_events (
                    id, approval_id, provider_event_id, event_type,
                    signature_valid, payload_sha256, payload_json
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    _identifier("approval_event"),
                    approval_id,
                    provider_event_id,
                    event_type,
                    signature_valid,
                    digest,
                    Jsonb(payload),
                ),
            )
            event_row = cur.fetchone()
            assert event_row is not None
            if signature_valid and target_status is not None and approval.status != target_status:
                cur.execute(
                    """
                    UPDATE approval_records
                    SET status = %s, approver_name = %s, decided_at = %s, updated_at = now()
                    WHERE id = %s
                    RETURNING *
                    """,
                    (target_status, approver_name, decided_at, approval_id),
                )
                approval_row = cur.fetchone()
                assert approval_row is not None
                approval = self._approval(approval_row)
            conn.commit()
        return ApprovalEventReceipt(
            event=self._event(event_row),
            approval=approval,
            duplicate=False,
        )

    def create_report_record(
        self,
        *,
        case_id: str,
        approval_id: str,
        object_key: str,
        sha256: str,
        metadata: dict[str, Any] | None = None,
    ) -> ReportRecord:
        _validate_digest(sha256)
        normalized_metadata = metadata or {}
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT case_id FROM approval_records WHERE id = %s", (approval_id,))
            approval = cur.fetchone()
            if approval is None or approval["case_id"] != case_id:
                raise ValueError("报告必须绑定本案件的审批记录")
            cur.execute(
                """
                INSERT INTO report_records (
                    id, case_id, approval_id, object_key, sha256, metadata_json
                ) VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (object_key) DO UPDATE
                SET object_key = EXCLUDED.object_key
                RETURNING *
                """,
                (
                    _identifier("report"),
                    case_id,
                    approval_id,
                    object_key,
                    sha256.lower(),
                    Jsonb(normalized_metadata),
                ),
            )
            row = cur.fetchone()
            assert row is not None
            report = self._report(row)
            if (
                report.case_id != case_id
                or report.approval_id != approval_id
                or report.sha256 != sha256.lower()
                or report.metadata != normalized_metadata
            ):
                raise ValueError("报告对象键已绑定其他记录")
            conn.commit()
        return report

    def get_report(self, report_id: str) -> ReportRecord | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM report_records WHERE id = %s", (report_id,))
            row = cur.fetchone()
        return self._report(row) if row is not None else None

    def list_reports(self, case_id: str) -> list[ReportRecord]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM report_records
                WHERE case_id = %s
                ORDER BY created_at, id
                """,
                (case_id,),
            )
            rows = cur.fetchall()
        return [self._report(row) for row in rows]

    def get_latest_report(self, case_id: str) -> ReportRecord | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM report_records
                WHERE case_id = %s
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (case_id,),
            )
            row = cur.fetchone()
        return self._report(row) if row is not None else None

    def _select_approval(
        self, column: Literal["id", "instance_id"], value: str
    ) -> ApprovalRecord | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(f"SELECT * FROM approval_records WHERE {column} = %s", (value,))
            row = cur.fetchone()
        return self._approval(row) if row is not None else None

    @staticmethod
    def _approval(row: dict[str, Any]) -> ApprovalRecord:
        return ApprovalRecord(
            id=row["id"],
            case_id=row["case_id"],
            task_id=row["task_id"],
            provider=row["provider"],
            instance_id=row["instance_id"],
            status=row["status"],
            approver_name=row["approver_name"],
            decided_at=_timestamp(row["decided_at"]) if row["decided_at"] is not None else None,
            payload=_json_copy(row["payload_json"] or {}),
            created_at=_timestamp(row["created_at"]),
            updated_at=_timestamp(row["updated_at"]),
        )

    @staticmethod
    def _event(row: dict[str, Any]) -> ApprovalEvent:
        return ApprovalEvent(
            id=row["id"],
            approval_id=row["approval_id"],
            provider_event_id=row["provider_event_id"],
            event_type=row["event_type"],
            signature_valid=row["signature_valid"],
            payload_sha256=row["payload_sha256"],
            payload=_json_copy(row["payload_json"]),
            created_at=_timestamp(row["created_at"]),
        )

    @staticmethod
    def _report(row: dict[str, Any]) -> ReportRecord:
        return ReportRecord(
            id=row["id"],
            case_id=row["case_id"],
            approval_id=row["approval_id"],
            object_key=row["object_key"],
            sha256=row["sha256"],
            metadata=_json_copy(row["metadata_json"] or {}),
            created_at=_timestamp(row["created_at"]),
        )

    @staticmethod
    def _delivery(row: dict[str, Any]) -> ApprovalDelivery:
        return ApprovalDelivery(
            id=row["id"],
            case_id=row["case_id"],
            task_id=row["task_id"],
            idempotency_key=row["idempotency_key"],
            status=row["status"],
            attempt_count=row["attempt_count"],
            instance_id=row["instance_id"],
            error_message=row["error_message"],
            lease_expires_at=(
                _timestamp(row["lease_expires_at"]) if row["lease_expires_at"] is not None else None
            ),
            created_at=_timestamp(row["created_at"]),
            updated_at=_timestamp(row["updated_at"]),
        )
