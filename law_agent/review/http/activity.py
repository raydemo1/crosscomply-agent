"""Case feedback, audit-event, and dashboard HTTP adapter."""

# FastAPI intentionally declares dependency providers in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException

from law_agent.review.case_store import CaseStore, UserRecord
from law_agent.review.http.schemas import FeedbackRequest


def register_activity_routes(
    app: FastAPI,
    *,
    current_user: Callable[..., Any],
    store: Callable[[], CaseStore],
    can_view: Callable[[UserRecord, dict[str, Any]], bool],
) -> None:
    router = APIRouter()

    @router.post("/api/cases/{identifier}/feedback")
    async def save_feedback(
        identifier: str,
        payload: FeedbackRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        feedback = store().save_feedback(identifier, user.id, **payload.model_dump(mode="json"))
        store().add_event(identifier, user.id, event_type="feedback_saved")
        return feedback

    @router.get("/api/cases/{identifier}/events")
    async def get_events(
        identifier: str,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        case = store().get_case(identifier)
        if case is None or not can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        return {"items": store().list_events(identifier)}

    @router.get("/api/dashboard/summary")
    async def dashboard_summary(user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        return store().dashboard_summary(user)

    app.include_router(router)


__all__ = ["register_activity_routes"]
