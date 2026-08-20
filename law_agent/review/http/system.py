"""Operational HTTP endpoints (health and service availability)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, FastAPI

from law_agent.config import load_llm_config, load_service_config
from law_agent.review.http.schemas import HealthResponse


def register_system_routes(app: FastAPI) -> None:
    router = APIRouter()

    @router.get("/api/health", response_model=HealthResponse)
    async def health_check() -> HealthResponse:
        llm_config = load_llm_config()
        try:
            from law_agent.review.retrieval.service_backends import healthcheck

            services = healthcheck(load_service_config())
        except Exception as exc:  # noqa: BLE001 - health must report degraded state
            services: dict[str, Any] = {
                "elasticsearch": False,
                "postgres": False,
                "error": str(exc),
            }
        chunks = Path(app.state.chunks_path)
        status = (
            "ok"
            if services.get("elasticsearch") and services.get("postgres") and llm_config.enabled
            else "degraded"
        )
        return HealthResponse(
            status=status,
            llm={
                "configured": llm_config.enabled,
                "reachable": llm_config.enabled,
                "model": llm_config.model,
                "base_url": llm_config.base_url,
            },
            services=services,
            corpus={
                "chunks_path": str(chunks),
                "chunks_file_exists": chunks.exists(),
                "indexed_count": {
                    "elasticsearch_docs": services.get("elasticsearch_docs", 0),
                    "pgvector_rows": services.get("pgvector_rows", 0),
                },
            },
        )

    app.include_router(router)


__all__ = ["register_system_routes"]
