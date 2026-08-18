"""Feishu approval boundary for the enterprise review workflow.

The module intentionally owns no persistence.  API code supplies an HTTP
transport and persists the returned instance/event records transactionally.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from base64 import b64decode
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from Crypto.Cipher import AES

from law_agent.review.workflow import ApprovalDecision, CaseStatus

FeishuInstanceStatus = Literal[
    "PENDING",
    "APPROVED",
    "REJECTED",
    "CANCELED",
    "DELETED",
]


class HttpResponse(Protocol):
    """Small response surface required from an injected HTTP client."""

    status_code: int

    def json(self) -> Any: ...


HttpTransport = Callable[..., HttpResponse]


@dataclass(frozen=True)
class FeishuApprovalConfig:
    app_id: str
    app_secret: str
    approval_code: str
    verification_token: str
    encrypt_key: str
    initiator_open_id: str = ""
    public_base_url: str = ""
    base_url: str = "https://open.feishu.cn/open-apis"
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class ApprovalInstance:
    instance_id: str
    request_id: str | None = None


@dataclass(frozen=True)
class VerifiedApprovalEvent:
    idempotency_key: str
    event_type: str
    instance_id: str
    decision: ApprovalDecision | None
    raw_status: str
    approver_id: str | None
    approval_time: str | None
    payload: Mapping[str, Any]


class FeishuApiError(RuntimeError):
    """A Feishu API request failed or returned an invalid response."""


class FeishuEventError(ValueError):
    """A callback failed authenticity or schema validation."""


class FeishuApprovalClient:
    """Minimal Feishu approval client with an injectable HTTP transport."""

    def __init__(self, config: FeishuApprovalConfig, transport: HttpTransport):
        self._config = config
        self._transport = transport

    def create_instance(
        self,
        *,
        form: list[Mapping[str, Any]],
        idempotency_key: str,
        user_id: str | None = None,
        open_id: str | None = None,
        department_id: str | None = None,
    ) -> ApprovalInstance:
        """Create one Feishu approval instance.

        ``idempotency_key`` is sent as the instance UUID.  Callers must persist
        it before retrying so a network retry cannot create a second approval.
        """

        token = self._tenant_access_token()
        if not user_id and not open_id:
            raise FeishuApiError("创建飞书审批实例必须提供发起人的 user_id 或 open_id")
        payload: dict[str, Any] = {
            "approval_code": self._config.approval_code,
            "form": json.dumps(form, ensure_ascii=False, separators=(",", ":")),
            "uuid": idempotency_key,
        }
        if user_id:
            payload["user_id"] = user_id
        if open_id:
            payload["open_id"] = open_id
        if department_id:
            payload["department_id"] = department_id

        response = self._transport(
            "POST",
            f"{self._config.base_url}/approval/v4/instances",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
            timeout=self._config.timeout_seconds,
        )
        body = _response_body(response, operation="创建飞书审批实例")
        data = body.get("data")
        instance_id = data.get("instance_code") if isinstance(data, Mapping) else None
        if not isinstance(instance_id, str) or not instance_id:
            raise FeishuApiError("创建飞书审批实例的响应缺少 instance_code")
        request_id = body.get("request_id")
        return ApprovalInstance(
            instance_id=instance_id,
            request_id=request_id if isinstance(request_id, str) else None,
        )

    def subscribe_approval_events(self) -> None:
        """Enable event delivery for the configured approval definition."""

        token = self._tenant_access_token()
        response = self._transport(
            "POST",
            f"{self._config.base_url}/approval/v4/approvals/{self._config.approval_code}/subscribe",
            headers={"Authorization": f"Bearer {token}"},
            json={},
            timeout=self._config.timeout_seconds,
        )
        _response_body(response, operation="订阅飞书审批事件")

    def _tenant_access_token(self) -> str:
        response = self._transport(
            "POST",
            f"{self._config.base_url}/auth/v3/tenant_access_token/internal",
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={
                "app_id": self._config.app_id,
                "app_secret": self._config.app_secret,
            },
            timeout=self._config.timeout_seconds,
        )
        body = _response_body(response, operation="获取飞书 tenant access token")
        token = body.get("tenant_access_token")
        if not isinstance(token, str) or not token:
            raise FeishuApiError("飞书鉴权响应缺少 tenant_access_token")
        return token


def verify_event_signature(
    *,
    body: bytes,
    timestamp: str,
    nonce: str,
    signature: str,
    encrypt_key: str,
) -> None:
    """Verify Feishu's SHA-256 callback signature in constant time."""

    if not all((timestamp, nonce, signature, encrypt_key)):
        raise FeishuEventError("飞书事件签名请求头不完整")
    expected = hashlib.sha256(
        timestamp.encode("utf-8") + nonce.encode("utf-8") + encrypt_key.encode("utf-8") + body
    ).hexdigest()
    if not hmac.compare_digest(expected, signature.lower()):
        raise FeishuEventError("飞书事件签名校验失败")


def parse_approval_event(
    *,
    body: bytes,
    timestamp: str,
    nonce: str,
    signature: str,
    config: FeishuApprovalConfig,
) -> VerifiedApprovalEvent:
    """Authenticate, decrypt, and normalize one Feishu approval callback."""

    verify_event_signature(
        body=body,
        timestamp=timestamp,
        nonce=nonce,
        signature=signature,
        encrypt_key=config.encrypt_key,
    )
    payload = decode_event_body(body=body, encrypt_key=config.encrypt_key)
    header = payload.get("header")
    event = payload.get("event")
    if not isinstance(event, Mapping):
        raise FeishuEventError("飞书事件缺少 event")
    normalized_header = header if isinstance(header, Mapping) else {}
    verification_token = normalized_header.get("token", payload.get("token"))
    if verification_token != config.verification_token:
        raise FeishuEventError("飞书事件 verification token 不匹配")
    event_id = normalized_header.get("event_id", payload.get("uuid"))
    event_type = normalized_header.get("event_type", event.get("type"))
    if not isinstance(event_type, str) or not event_type:
        raise FeishuEventError("飞书事件缺少 event_type")

    instance = _event_instance(event)
    instance_id = _first_string(instance, "instance_code", "instance_id")
    raw_status = (_first_string(instance, "status") or "").upper()
    if not instance_id or not raw_status:
        raise FeishuEventError("飞书审批事件缺少实例 ID 或状态")
    conditionally_approved = bool(instance.get("conditionally_approved", False))
    decision = (
        map_feishu_decision(raw_status, conditionally_approved=conditionally_approved)
        if raw_status in {"APPROVED", "REJECTED", "CANCELED", "DELETED", "WITHDRAWN", "REVERTED"}
        else None
    )

    stable_event_id = event_id if isinstance(event_id, str) and event_id else None
    idempotency_key = stable_event_id or hashlib.sha256(body).hexdigest()
    approver_id = _first_string(instance, "approver_id", "user_id", "open_id")
    approval_time = _normalize_approval_time(
        _first_string(
            instance,
            "approval_time",
            "instance_operate_time",
            "operate_time",
            "end_time",
            "update_time",
        )
    )
    return VerifiedApprovalEvent(
        idempotency_key=idempotency_key,
        event_type=event_type,
        instance_id=instance_id,
        decision=decision,
        raw_status=raw_status,
        approver_id=approver_id,
        approval_time=approval_time,
        payload=payload,
    )


def _normalize_approval_time(value: str | None) -> str | None:
    """Normalize Feishu's ISO or millisecond timestamps for PostgreSQL."""

    if not value:
        return None
    if value.isdigit():
        timestamp = int(value)
        seconds = timestamp / 1000 if timestamp >= 10_000_000_000 else timestamp
        try:
            return datetime.fromtimestamp(seconds, tz=UTC).isoformat()
        except (OverflowError, OSError, ValueError):
            raise FeishuEventError("飞书审批事件时间戳无效") from None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise FeishuEventError("飞书审批事件时间格式无效") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.isoformat()


def decode_event_body(*, body: bytes, encrypt_key: str) -> Mapping[str, Any]:
    """Decode a signed Feishu event envelope, including AES encrypted bodies."""

    try:
        outer = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeishuEventError("飞书事件不是合法 JSON") from exc
    if not isinstance(outer, Mapping):
        raise FeishuEventError("飞书事件必须是 JSON object")
    encrypted = outer.get("encrypt")
    if encrypted is None:
        return outer
    if not isinstance(encrypted, str) or not encrypted or not encrypt_key:
        raise FeishuEventError("飞书加密事件缺少有效密文或 Encrypt Key")
    try:
        encrypted_bytes = b64decode(encrypted, validate=True)
        if len(encrypted_bytes) <= AES.block_size:
            raise ValueError("ciphertext too short")
        iv = encrypted_bytes[: AES.block_size]
        cipher = AES.new(hashlib.sha256(encrypt_key.encode("utf-8")).digest(), AES.MODE_CBC, iv)
        padded = cipher.decrypt(encrypted_bytes[AES.block_size :])
        padding = padded[-1]
        if (
            padding < 1
            or padding > AES.block_size
            or padded[-padding:] != bytes([padding]) * padding
        ):
            raise ValueError("invalid padding")
        decoded = json.loads(padded[:-padding].decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise FeishuEventError("飞书加密事件解密失败") from exc
    if not isinstance(decoded, Mapping):
        raise FeishuEventError("飞书解密事件必须是 JSON object")
    return decoded


def map_feishu_decision(
    status: str,
    *,
    conditionally_approved: bool = False,
) -> ApprovalDecision:
    """Map an approval terminal state into CrossComply's decision vocabulary."""

    normalized = status.upper()
    if normalized == "APPROVED":
        return "conditionally_approved" if conditionally_approved else "approved"
    if normalized == "REJECTED":
        return "rejected"
    if normalized in {"CANCELED", "DELETED", "WITHDRAWN", "REVERTED"}:
        return "withdrawn"
    raise FeishuEventError(f"飞书审批状态 {status!r} 不是可回写的终态")


def apply_authoritative_decision(
    *,
    current_status: CaseStatus,
    decision: ApprovalDecision,
) -> CaseStatus:
    """Apply Feishu authority without allowing terminal state replacement."""

    target: CaseStatus = {
        "approved": "approved",
        "conditionally_approved": "conditionally_approved",
        "rejected": "rejected",
        "withdrawn": "rejected",
    }[decision]
    terminal = {"approved", "conditionally_approved", "rejected"}
    if current_status in terminal:
        if current_status != target:
            raise FeishuEventError(f"案件已有不可逆审批终态 {current_status}，不能覆盖为 {target}")
        return current_status
    if current_status != "pending_feishu_approval":
        raise FeishuEventError(f"案件状态 {current_status} 不能接收飞书审批终态 {target}")
    return target


def _response_body(response: HttpResponse, *, operation: str) -> Mapping[str, Any]:
    try:
        body = response.json()
    except Exception as exc:  # pragma: no cover - transport-specific exception types
        raise FeishuApiError(f"{operation}返回了无效 JSON") from exc
    if not isinstance(body, Mapping):
        raise FeishuApiError(f"{operation}返回值不是 JSON object")
    code = body.get("code", 0)
    if response.status_code >= 400 or code not in (0, "0", None):
        message = body.get("msg") or body.get("message") or "unknown error"
        raise FeishuApiError(
            f"{operation}失败: HTTP {response.status_code}, code={code}, message={message}"
        )
    return body


def _event_instance(event: Mapping[str, Any]) -> Mapping[str, Any]:
    nested = event.get("instance")
    return nested if isinstance(nested, Mapping) else event


def _first_string(value: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None
