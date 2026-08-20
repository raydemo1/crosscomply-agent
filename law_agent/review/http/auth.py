"""Session authentication HTTP adapter."""

# FastAPI intentionally declares dependency providers in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from law_agent.review.case_store import CaseStore, UserRecord
from law_agent.review.http.schemas import LoginRequest

SESSION_COOKIE = "crosscomply_session"


def register_auth_routes(
    app: FastAPI,
    *,
    current_user: Callable[..., Any],
    store: Callable[[], CaseStore],
) -> None:
    """Register login, logout, and current-session endpoints."""

    router = APIRouter()

    @router.post("/api/auth/login")
    async def login(payload: LoginRequest) -> JSONResponse:
        user = store().authenticate(payload.username, payload.password)
        if user is None:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        try:
            ttl_hours = max(1, int(os.getenv("CROSSCOMPLY_SESSION_TTL_HOURS", "12")))
            token, expires_at = store().create_session(user.id, ttl_hours)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="案件数据库尚未完成初始化或未配置初始账号",
            ) from exc
        response = JSONResponse({"user": user.to_dict(), "expires_at": expires_at})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=ttl_hours * 3600,
            httponly=True,
            samesite="lax",
            secure=os.getenv("CROSSCOMPLY_COOKIE_SECURE", "false").lower() == "true",
            path="/",
        )
        return response

    @router.post("/api/auth/logout")
    async def logout(request: Request) -> JSONResponse:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            store().delete_session(token)
        response = JSONResponse({"ok": True})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @router.get("/api/auth/me")
    async def me(user: UserRecord = Depends(current_user)) -> dict[str, Any]:
        return {"user": user.to_dict()}

    app.include_router(router)


__all__ = ["SESSION_COOKIE", "register_auth_routes"]
