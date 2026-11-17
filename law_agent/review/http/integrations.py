"""External approval and Feishu integration HTTP adapter."""

# FastAPI intentionally declares dependency providers in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request

from law_agent.review.approval_readiness import ApprovalReadiness, assess_approval_readiness
from law_agent.review.case_store import CaseStore, UserRecord
from law_agent.review.enterprise_store import (
    InMemoryEnterpriseStore,
    PostgresEnterpriseStore,
    ReviewTask,
)
from law_agent.review.feishu import (
    FeishuEventError,
    apply_authoritative_decision,
    decode_event_body,
    parse_approval_event,
)
from law_agent.review.governance_store import InMemoryGovernanceStore, PostgresGovernanceStore


def register_integration_routes(
    app: FastAPI,
    *,
    current_user: Callable[..., Any],
    admin_only: Callable[[UserRecord], None],
    reviewer_only: Callable[[UserRecord], None],
    store: Callable[[], CaseStore],
    enterprise: Callable[[], InMemoryEnterpriseStore | PostgresEnterpriseStore],
    governance: Callable[[], InMemoryGovernanceStore | PostgresGovernanceStore],
    configured_feishu: Callable[[], tuple[Any, Any]],
    deliver_feishu_approval: Callable[..., dict[str, Any]],
    remediation_event: Callable[..., None],
    transfer_review: Callable[[str], dict[str, Any] | None],
) -> None:
    router = APIRouter()

    def readiness_for(case: dict[str, Any], task: ReviewTask | None, *, approval_exists: bool) -> ApprovalReadiness:
        identifier = case["id"]
        records = enterprise()
        snapshot = records.get_latest_material_snapshot(identifier)
        intake = records.get_latest_intake_snapshot(case_id=identifier, material_snapshot_id=snapshot.id) if snapshot else None
        transfer = transfer_review(identifier)
        return assess_approval_readiness(
            case=case, task=task, plan=store().get_remediation_plan(identifier),
            material_snapshot_id=snapshot.id if snapshot else None,
            intake_snapshot_id=intake.id if intake else None,
            approval_exists=approval_exists,
            transfer_impact_required=bool(transfer and task and (transfer.get("impact") or {}).get("task_id") != task.id),
        )

    def require_ready(readiness: ApprovalReadiness) -> None:
        if readiness.code == "actions_open":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "pre_approval_actions_open",
                    "message": "送审前行动项尚未全部完成",
                    "action_ids": readiness.action_ids,
                },
            )
        if readiness.code in {"fact_review", "evidence_review", "source_review"}:
            raise HTTPException(status_code=409, detail="审查仍有事实或证据缺口，不能发起飞书审批")
        if readiness.code != "ready":
            raise HTTPException(status_code=409, detail=readiness.detail or "当前案件状态不能发起飞书审批")

    @router.post("/api/cases/{identifier}/feishu-approval")
    async def create_feishu_approval(
        identifier: str,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        reviewer_only(user)
        case = store().get_case(identifier)
        if case is None:
            raise HTTPException(status_code=404, detail="案件不存在")
        task = enterprise().get_latest_task(identifier)
        existing = governance().get_latest_approval(identifier)
        readiness = readiness_for(case, task, approval_exists=existing is not None)
        if readiness.code == "submitted" and existing:
            return asdict(existing)
        require_ready(readiness)
        assert task is not None
        governance().requeue_expired_approval_deliveries(target_status="failed")
        delivery = governance().enqueue_approval_delivery(
            case_id=identifier,
            task_id=task.id,
            idempotency_key=f"{identifier}:{task.id}",
        )
        if delivery.status == "running":
            raise HTTPException(status_code=409, detail="飞书审批投递正在处理中，请稍后重试")
        if delivery.status == "failed":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "feishu_delivery_requires_retry",
                    "delivery_id": delivery.id,
                    "message": delivery.error_message,
                },
            )
        if delivery.status == "succeeded" and delivery.instance_id:
            approval = governance().get_approval_by_instance(delivery.instance_id)
            if approval is not None:
                return asdict(approval)
        return deliver_feishu_approval(case=case, task=task, user=user, delivery=delivery)

    @router.post("/api/approval-deliveries/{delivery_id}/retry")
    async def retry_feishu_approval_delivery(
        delivery_id: str,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        reviewer_only(user)
        governance().requeue_expired_approval_deliveries(target_status="failed")
        delivery = governance().get_approval_delivery(delivery_id)
        if delivery is None:
            raise HTTPException(status_code=404, detail="飞书审批投递任务不存在")
        case = store().get_case(delivery.case_id)
        task = enterprise().get_task(delivery.task_id)
        if case is None or task is None:
            raise HTTPException(status_code=409, detail="投递任务绑定的案件或审查任务不存在")
        latest = enterprise().get_latest_task(case["id"])
        if latest is None or latest.id != task.id:
            raise HTTPException(status_code=409, detail="审批投递绑定的审查任务已过期")
        require_ready(readiness_for(case, task, approval_exists=False))
        try:
            queued = governance().retry_approval_delivery(delivery_id)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return deliver_feishu_approval(case=case, task=task, user=user, delivery=queued)

    @router.post("/api/integrations/feishu/approval-events")
    async def receive_feishu_approval_event(request: Request) -> dict[str, Any]:
        _, config = configured_feishu()
        body = await request.body()
        try:
            decoded = decode_event_body(body=body, encrypt_key=config.encrypt_key)
        except FeishuEventError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if decoded.get("type") == "url_verification":
            if decoded.get("token") != config.verification_token:
                raise HTTPException(status_code=401, detail="飞书 verification token 不匹配")
            challenge = decoded.get("challenge")
            if not isinstance(challenge, str) or not challenge:
                raise HTTPException(status_code=400, detail="飞书 URL 校验缺少 challenge")
            return {"challenge": challenge}
        try:
            event = parse_approval_event(
                body=body,
                timestamp=request.headers.get("x-lark-request-timestamp", ""),
                nonce=request.headers.get("x-lark-request-nonce", ""),
                signature=request.headers.get("x-lark-signature", ""),
                config=config,
            )
        except FeishuEventError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        if event.decision is None:
            return {"ok": True, "ignored": True, "status": event.raw_status}
        approval = governance().get_approval_by_instance(event.instance_id)
        if approval is None:
            raise HTTPException(status_code=404, detail="飞书审批实例未绑定 CrossComply 案件")
        receipt = governance().record_approval_event(
            approval_id=approval.id,
            provider_event_id=event.idempotency_key,
            event_type=event.event_type,
            signature_valid=True,
            payload=dict(event.payload),
            target_status=event.decision,
            approver_name=event.approver_id,
            decided_at=event.approval_time,
        )
        case = store().get_case(approval.case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="审批绑定的案件不存在")
        try:
            target = apply_authoritative_decision(
                current_status=case["status"],
                decision=event.decision,
            )
        except FeishuEventError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if case["status"] != target:
            store().update_case(approval.case_id, status=target)
            store().add_event(
                approval.case_id,
                case.get("owner_id") or case["created_by"],
                event_type="feishu_decision_written_back",
                from_status=case["status"],
                to_status=target,
                payload={"approval_event_id": receipt.event.id},
            )
        if target in {"approved", "conditionally_approved"}:
            remediation_plan = store().get_remediation_plan(approval.case_id)
            if remediation_plan is not None and remediation_plan["status"] == "draft":
                try:
                    store().activate_remediation_plan(remediation_plan["id"])
                    store().add_event(
                        approval.case_id,
                        case.get("owner_id") or case["created_by"],
                        event_type="remediation_plan_activated",
                        payload={"plan_id": remediation_plan["id"], "trigger": "feishu_decision"},
                    )
                    remediation_event(
                        remediation_plan["id"],
                        case.get("owner_id") or case["created_by"],
                        event_type="plan_activated",
                        payload={"trigger": "feishu_decision"},
                    )
                except ValueError:
                    pass
        return {
            "ok": True,
            "duplicate": receipt.duplicate,
            "case_status": target,
            "report_status": "available_on_download",
        }

    @router.post("/api/integrations/feishu/subscribe")
    async def subscribe_feishu_approval_events(
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        client, config = configured_feishu()
        try:
            client.subscribe_approval_events()
        except Exception as exc:
            raise HTTPException(status_code=503, detail=str(exc) or exc.__class__.__name__) from exc
        return {"ok": True, "approval_code": config.approval_code}

    app.include_router(router)


__all__ = ["register_integration_routes"]
