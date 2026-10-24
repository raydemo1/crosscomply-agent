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


class _LoginThrottle:
    """进程内登录失败退避：5 次失败后指数锁定，防在线暴破。"""

    def __init__(self) -> None:
        self._failures: dict[str, list[float]] = {}
        self._locked_until: dict[str, float] = {}

    def check(self, key: str) -> None:
        import time

        locked = self._locked_until.get(key, 0.0)
        if locked > time.time():
            wait = int(locked - time.time()) + 1
            raise HTTPException(status_code=429, detail=f"失败次数过多，请 {wait} 秒后重试")

    def record_failure(self, key: str) -> None:
        import time

        now = time.time()
        bucket = [t for t in self._failures.get(key, []) if now - t < 900]
        bucket.append(now)
        self._failures[key] = bucket
        if len(bucket) >= 5:
            backoff = min(60 * (2 ** (len(bucket) - 5)), 900)
            self._locked_until[key] = now + backoff
            self._failures[key] = []

    def clear(self, key: str) -> None:
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)


_login_throttle = _LoginThrottle()


def _cookie_secure(request: Request) -> bool:
    """显式配置优先；否则 HTTPS（含反代）下自动开启。"""

    configured = os.getenv("CROSSCOMPLY_COOKIE_SECURE")
    if configured is not None:
        return configured.lower() == "true"
    forwarded = request.headers.get("x-forwarded-proto", "")
    return request.url.scheme == "https" or forwarded.split(",")[0].strip() == "https"


def register_auth_routes(
    app: FastAPI,
    *,
    current_user: Callable[..., Any],
    store: Callable[[], CaseStore],
) -> None:
    """Register login, logout, and current-session endpoints."""

    router = APIRouter()

    @router.post("/api/auth/login")
    async def login(payload: LoginRequest, request: Request) -> JSONResponse:
        client_key = f"{payload.username}@{request.client.host if request.client else '?'}"
        _login_throttle.check(client_key)
        user = store().authenticate(payload.username, payload.password)
        if user is None:
            _login_throttle.record_failure(client_key)
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        try:
            ttl_hours = max(1, int(os.getenv("CROSSCOMPLY_SESSION_TTL_HOURS", "12") or "12"))
            token, expires_at = store().create_session(user.id, ttl_hours)
        except Exception as exc:
            raise HTTPException(
                status_code=503,
                detail="案件数据库尚未完成初始化或未配置初始账号",
            ) from exc
        _login_throttle.clear(client_key)
        response = JSONResponse({"user": user.to_dict(), "expires_at": expires_at})
        response.set_cookie(
            SESSION_COOKIE,
            token,
            max_age=ttl_hours * 3600,
            httponly=True,
            samesite="lax",
            secure=_cookie_secure(request),
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
