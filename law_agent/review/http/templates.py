"""Case-template HTTP adapter."""

# FastAPI intentionally declares dependency providers in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException

from law_agent.review.case_store import CaseStore, UserRecord
from law_agent.review.http.schemas import CaseTemplateCreateRequest, CaseTemplateUpdateRequest
from law_agent.review.template_store import TemplateStore


def register_template_routes(
    app: FastAPI,
    *,
    current_user: Callable[..., Any],
    store: Callable[[], CaseStore],
    templates: Callable[[], TemplateStore],
) -> None:
    """Register CRUD and archive operations for reusable case templates."""

    router = APIRouter()

    def template_payload(item: dict[str, Any]) -> dict[str, Any]:
        payload = dict(item)
        creator = store().get_user(item.get("created_by", ""))
        payload["created_by_user"] = creator.to_dict() if creator else None
        return payload

    @router.get("/api/case-templates")
    async def list_case_templates(
        query: str | None = None,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        items = templates().list_templates(user, query)
        return {"items": [template_payload(item) for item in items], "total": len(items)}

    @router.post("/api/case-templates")
    async def create_case_template(
        payload: CaseTemplateCreateRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            item = templates().create_template(
                user,
                name=payload.name,
                description=payload.description,
                question=payload.question,
                intake=payload.intake,
                review_mode=payload.review_mode,
                rerank_mode=payload.rerank_mode,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return template_payload(item)

    @router.patch("/api/case-templates/{identifier}")
    async def update_case_template(
        identifier: str,
        payload: CaseTemplateUpdateRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        values = payload.model_dump(exclude_unset=True)
        if not values:
            raise HTTPException(status_code=422, detail="至少提供一项需要修改的模板字段")
        if "name" in values and not str(values["name"]).strip():
            raise HTTPException(status_code=422, detail="模板名称不能为空")
        if "question" in values and not str(values["question"]).strip():
            raise HTTPException(status_code=422, detail="审查问题不能为空")
        if "description" in values and values["description"] is None:
            values["description"] = ""
        if "name" in values:
            values["name"] = str(values["name"]).strip()
        if "question" in values:
            values["question"] = str(values["question"]).strip()
        try:
            item = templates().update_template(identifier, user, **values)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="使用模板不存在") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return template_payload(item)

    @router.post("/api/case-templates/{identifier}/archive")
    async def archive_case_template(
        identifier: str,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        try:
            item = templates().archive_template(identifier, user)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="使用模板不存在") from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return template_payload(item)

    app.include_router(router)


__all__ = ["register_template_routes"]
