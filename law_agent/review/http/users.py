"""Administrator user-management HTTP adapter."""

# FastAPI intentionally declares dependency providers in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException

from law_agent.review.case_store import UserRecord
from law_agent.review.http.schemas import (
    UserCreateRequest,
    UserPasswordResetRequest,
    UserRoleRequest,
    UserStateRequest,
)
from law_agent.review.user_admin import UserAdminError, UserAdminStore


def register_user_routes(
    app: FastAPI,
    *,
    current_user: Callable[..., Any],
    admin_only: Callable[[UserRecord], None],
    user_admin: Callable[[], UserAdminStore],
) -> None:
    """Register administrator-only user lifecycle endpoints."""

    router = APIRouter()

    @router.get("/api/admin/users")
    async def list_managed_users(
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        items = user_admin().list_users()
        return {"items": [asdict(item) for item in items], "total": len(items)}

    @router.post("/api/admin/users")
    async def create_managed_user(
        payload: UserCreateRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        try:
            created = user_admin().create_user(
                payload.username,
                payload.display_name,
                payload.password,
                payload.role,
            )
        except UserAdminError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return asdict(created)

    @router.patch("/api/admin/users/{user_id}/state")
    async def set_managed_user_state(
        user_id: str,
        payload: UserStateRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        try:
            updated = user_admin().set_active(user_id, payload.active)
        except UserAdminError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return asdict(updated)

    @router.post("/api/admin/users/{user_id}/reset-password")
    async def reset_managed_user_password(
        user_id: str,
        payload: UserPasswordResetRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        try:
            updated = user_admin().reset_password(user_id, payload.password)
        except UserAdminError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return asdict(updated)

    @router.patch("/api/admin/users/{user_id}/role")
    async def assign_managed_user_role(
        user_id: str,
        payload: UserRoleRequest,
        user: UserRecord = Depends(current_user),
    ) -> dict[str, Any]:
        admin_only(user)
        try:
            updated = user_admin().assign_role(user_id, payload.role)
        except UserAdminError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return asdict(updated)

    app.include_router(router)


__all__ = ["register_user_routes"]
