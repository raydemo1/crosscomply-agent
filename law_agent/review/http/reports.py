"""Decision-report generation and download HTTP adapter."""

# FastAPI intentionally declares dependency providers in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import Response

from law_agent.review.case_store import CaseStore, UserRecord
from law_agent.review.enterprise_store import InMemoryEnterpriseStore, PostgresEnterpriseStore
from law_agent.review.governance_store import InMemoryGovernanceStore, PostgresGovernanceStore
from law_agent.review.object_store import MaterialObjectStore
from law_agent.review.report_service import ensure_decision_report


def register_report_routes(
    app: FastAPI,
    *,
    current_user: Callable[..., Any],
    reviewer_only: Callable[[UserRecord], None],
    store: Callable[[], CaseStore],
    enterprise: Callable[[], InMemoryEnterpriseStore | PostgresEnterpriseStore],
    governance: Callable[[], InMemoryGovernanceStore | PostgresGovernanceStore],
    originals: Callable[[], MaterialObjectStore],
    can_view: Callable[[UserRecord, dict[str, Any]], bool],
) -> None:
    router = APIRouter()

    def report_response(report: Any) -> Response:
        case = store().get_case(report.case_id)
        if case is None:
            raise HTTPException(status_code=404, detail="决策报告所属案件不存在")
        content = originals().get_original(report.object_key)
        if hashlib.sha256(content).hexdigest() != report.sha256:
            raise HTTPException(status_code=409, detail="决策报告哈希校验失败")
        return Response(
            content=content,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{case["case_number"]}.pdf"'},
        )

    @router.post("/api/cases/{identifier}/reports")
    async def create_decision_report(
        identifier: str,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        reviewer_only(user)
        case = store().get_case(identifier)
        if case is None:
            raise HTTPException(status_code=404, detail="案件不存在")
        try:
            report = ensure_decision_report(
                case=case,
                case_store=store(),
                enterprise_store=enterprise(),
                governance_store=governance(),
                object_store=originals(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail="正式报告暂未生成，请稍后重试") from exc
        return asdict(report)

    @router.get("/api/cases/{identifier}/reports/download")
    async def download_case_decision_report(
        identifier: str,
        user: UserRecord = Depends(current_user),
    ) -> Response:
        case = store().get_case(identifier)
        if case is None or not can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        report = governance().get_latest_report(identifier)
        if report is None:
            try:
                report = ensure_decision_report(
                    case=case,
                    case_store=store(),
                    enterprise_store=enterprise(),
                    governance_store=governance(),
                    object_store=originals(),
                )
            except ValueError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except HTTPException:
                raise
            except Exception as exc:
                prior_failure = any(
                    item["event_type"] == "decision_report_generation_failed"
                    and item.get("payload", {}).get("trigger") == "download"
                    for item in store().list_events(identifier)
                )
                if not prior_failure:
                    store().add_event(
                        identifier,
                        user.id,
                        event_type="decision_report_generation_failed",
                        payload={"trigger": "download", "message": str(exc)[:500]},
                    )
                raise HTTPException(status_code=503, detail="正式报告暂未生成，请稍后重试") from exc
            store().add_event(
                identifier,
                user.id,
                event_type="report_generated",
                payload={"report_id": report.id, "trigger": "download"},
            )
        return report_response(report)

    @router.get("/api/reports/{report_id}/download")
    async def download_decision_report(
        report_id: str,
        user: UserRecord = Depends(current_user),
    ) -> Response:
        report = governance().get_report(report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="决策报告不存在")
        case = store().get_case(report.case_id)
        if case is None or not can_view(user, case):
            raise HTTPException(status_code=404, detail="决策报告不存在或无权访问")
        return report_response(report)

    app.include_router(router)


__all__ = ["register_report_routes"]
