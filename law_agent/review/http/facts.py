"""Applicant-only fact confirmation adapters."""

# ruff: noqa: B008
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from law_agent.review.fact_confirmation import confirm_facts, stage_fact_answer
from law_agent.review.http.schemas import FactAnswerRequest, FactConfirmRequest


def register_fact_routes(app, *, current_user, store, enterprise):
    router = APIRouter()

    def invoke(action, case_id, payload, user):
        try:
            return action(cases=store(), enterprise=enterprise(), user=user, case_id=case_id, payload=payload)
        except (PermissionError, KeyError) as exc:
            raise HTTPException(status_code=404, detail="案件不存在或无权确认事实") from exc
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="事实值的类型或选项不符合填报要求") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.post("/api/cases/{case_id}/fact-answers")
    async def answer(case_id: str, payload: FactAnswerRequest, user=Depends(current_user)):
        return invoke(stage_fact_answer, case_id, payload, user)

    @router.post("/api/cases/{case_id}/confirm-facts")
    async def confirm(case_id: str, payload: FactConfirmRequest, user=Depends(current_user)):
        return JSONResponse(status_code=202, content=invoke(confirm_facts, case_id, payload, user))

    app.include_router(router)
