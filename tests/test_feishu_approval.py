from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

import pytest
from Crypto.Cipher import AES

from law_agent.review.feishu import (
    FeishuApprovalClient,
    FeishuApprovalConfig,
    FeishuEventError,
    apply_authoritative_decision,
    decode_event_body,
    parse_approval_event,
)


@dataclass
class FakeResponse:
    status_code: int
    body: dict[str, Any]

    def json(self) -> dict[str, Any]:
        return self.body


def config() -> FeishuApprovalConfig:
    return FeishuApprovalConfig(
        app_id="app-id",
        app_secret="secret",
        approval_code="approval-code",
        verification_token="verify-token",
        encrypt_key="encrypt-key",
        initiator_open_id="ou_initiator",
    )


def test_create_approval_uses_uuid_for_idempotency() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def transport(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append((method, url, kwargs))
        if url.endswith("tenant_access_token/internal"):
            return FakeResponse(200, {"code": 0, "tenant_access_token": "token"})
        return FakeResponse(
            200,
            {"code": 0, "data": {"instance_code": "instance-1"}},
        )

    result = FeishuApprovalClient(config(), transport).create_instance(
        user_id="employee-1",
        form=[{"id": "case", "value": "CASE-001"}],
        idempotency_key="case-snapshot-rule-v1",
    )

    assert result.instance_id == "instance-1"
    assert calls[1][2]["json"]["uuid"] == "case-snapshot-rule-v1"
    assert calls[1][2]["headers"]["Authorization"] == "Bearer token"


def test_subscribe_approval_events_uses_configured_definition() -> None:
    calls: list[tuple[str, str, dict[str, Any]]] = []

    def transport(method: str, url: str, **kwargs: Any) -> FakeResponse:
        calls.append((method, url, kwargs))
        if url.endswith("tenant_access_token/internal"):
            return FakeResponse(200, {"code": 0, "tenant_access_token": "token"})
        return FakeResponse(200, {"code": 0, "data": {}})

    FeishuApprovalClient(config(), transport).subscribe_approval_events()

    assert calls[1][0] == "POST"
    assert calls[1][1].endswith("/approval/v4/approvals/approval-code/subscribe")


def test_legacy_approval_instance_event_is_supported() -> None:
    body = json.dumps(
        {
            "uuid": "legacy-event-1",
            "token": "verify-token",
            "type": "event_callback",
            "event": {
                "type": "approval_instance",
                "instance_code": "instance-legacy",
                "status": "REJECTED",
                "user_id": "reviewer-legacy",
                "instance_operate_time": "1787040000000",
            },
        },
        separators=(",", ":"),
    ).encode()
    signature = hashlib.sha256(b"123nonceencrypt-key" + body).hexdigest()

    event = parse_approval_event(
        body=body,
        timestamp="123",
        nonce="nonce",
        signature=signature,
        config=config(),
    )

    assert event.idempotency_key == "legacy-event-1"
    assert event.instance_id == "instance-legacy"
    assert event.decision == "rejected"
    assert event.approval_time == "2026-08-18T08:00:00+00:00"


def test_encrypted_event_body_is_decrypted_with_configured_key() -> None:
    plaintext = json.dumps(
        {"type": "url_verification", "token": "verify-token", "challenge": "challenge-1"},
        separators=(",", ":"),
    ).encode()
    padding = AES.block_size - len(plaintext) % AES.block_size
    padded = plaintext + bytes([padding]) * padding
    iv = b"0123456789abcdef"
    cipher = AES.new(hashlib.sha256(b"encrypt-key").digest(), AES.MODE_CBC, iv)
    import base64

    body = json.dumps({"encrypt": base64.b64encode(iv + cipher.encrypt(padded)).decode()}).encode()

    decoded = decode_event_body(body=body, encrypt_key="encrypt-key")

    assert decoded["challenge"] == "challenge-1"


def _signed_event(status: str = "APPROVED") -> tuple[bytes, str]:
    body = json.dumps(
        {
            "token": "verify-token",
            "header": {
                "event_id": "event-1",
                "event_type": "approval_instance",
            },
            "event": {
                "instance_code": "instance-1",
                "status": status,
                "approver_id": "reviewer-1",
                "approval_time": "2026-08-18T12:00:00+08:00",
            },
        },
        separators=(",", ":"),
    ).encode()
    signature = hashlib.sha256(b"123nonceencrypt-key" + body).hexdigest()
    return body, signature


def test_event_signature_and_event_id_are_verified() -> None:
    body, signature = _signed_event()
    event = parse_approval_event(
        body=body,
        timestamp="123",
        nonce="nonce",
        signature=signature,
        config=config(),
    )

    assert event.idempotency_key == "event-1"
    assert event.instance_id == "instance-1"
    assert event.decision == "approved"
    assert event.approver_id == "reviewer-1"

    duplicate = parse_approval_event(
        body=body,
        timestamp="123",
        nonce="nonce",
        signature=signature,
        config=config(),
    )
    assert duplicate.idempotency_key == event.idempotency_key


@pytest.mark.parametrize(
    ("status", "decision"),
    [
        ("APPROVED", "approved"),
        ("REJECTED", "rejected"),
        ("CANCELED", "withdrawn"),
    ],
)
def test_feishu_terminal_status_mapping(status: str, decision: str) -> None:
    body, signature = _signed_event(status)
    event = parse_approval_event(
        body=body,
        timestamp="123",
        nonce="nonce",
        signature=signature,
        config=config(),
    )
    assert event.decision == decision


def test_invalid_event_signature_is_rejected() -> None:
    body, _ = _signed_event()
    with pytest.raises(FeishuEventError, match="签名校验失败"):
        parse_approval_event(
            body=body,
            timestamp="123",
            nonce="nonce",
            signature="0" * 64,
            config=config(),
        )


def test_terminal_approval_cannot_be_overwritten() -> None:
    assert (
        apply_authoritative_decision(
            current_status="pending_feishu_approval",
            decision="approved",
        )
        == "approved"
    )
    assert (
        apply_authoritative_decision(current_status="approved", decision="approved") == "approved"
    )
    with pytest.raises(FeishuEventError, match="不可逆"):
        apply_authoritative_decision(current_status="approved", decision="rejected")
