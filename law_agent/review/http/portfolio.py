"""Annual transfer portfolio and reviewer acknowledgements."""

# ruff: noqa: B008

from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from law_agent.review.dates import business_today
from law_agent.review.http.transfers import Note
from law_agent.review.portfolio import Band, crosses_threshold

YearQuery = Annotated[int | None, Query(ge=1, le=9999)]


class AcknowledgePortfolio(BaseModel):
    year: int = Field(ge=1, le=9999)
    band: Band
    note: Note


def register_portfolio_routes(app, *, current_user, portfolio, reviewer_only):
    router = APIRouter()

    @router.get("/api/entities/portfolio")
    def summaries(year: YearQuery = None, user=Depends(current_user)):
        service = portfolio()
        items = service.summaries(user, year or business_today().year)
        return {"items": [_summary_response(service, item, user) for item in items]}

    @router.get("/api/entities/{key:path}/portfolio")
    def summary(key: str, year: YearQuery = None, user=Depends(current_user)):
        service = portfolio()
        items = service.summaries(user, year or business_today().year, key=key)
        if not items:
            raise HTTPException(status_code=404, detail="主体不存在或无权访问")
        return _summary_response(service, items[0], user)

    @router.post("/api/entities/{key:path}/portfolio/ack")
    def ack(key: str, payload: AcknowledgePortfolio, user=Depends(current_user)):
        reviewer_only(user)
        service = portfolio()
        items = service.summaries(user, payload.year, key=key)
        if not items:
            raise HTTPException(status_code=404, detail="主体不存在或无权访问")
        item = items[0]
        if not crosses_threshold(item) or item.band_upper != payload.band:
            raise HTTPException(status_code=409, detail="累计档位已变化，请刷新后核对")
        service.transfers.ack_portfolio(
            key, item.year, item.band_upper, user.id, payload.note,
            {row.baseline_case_id for row in item.contributions},
        )
        return {"confirmed": True}

    @router.get("/api/cases/{case_id}/portfolio-snapshot")
    def snapshot(case_id: str, user=Depends(current_user)):
        service = portfolio()
        case = service.cases.get_case(case_id)
        if case is None or (user.role == "requester" and user.id != (case.get("owner_id") or case["created_by"])):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")
        task = service.enterprise.get_latest_task(case_id)
        frozen = service.enterprise.get_portfolio_snapshot(task.portfolio_snapshot_id) if task and task.portfolio_snapshot_id else None
        if frozen is None or frozen.case_id != case_id:
            raise HTTPException(status_code=404, detail="没有冻结的主体累计台账")
        return asdict(frozen)

    app.include_router(router)


def _summary_response(service, item, user):
    alert = user.role != "requester" and crosses_threshold(item) and not service.acked(
        item.entity_key, item.year, item.band_upper,
    )
    return {**asdict(item), "threshold_alert": alert}
