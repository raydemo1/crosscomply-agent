"""Evaluation control-plane HTTP adapter."""

# FastAPI intentionally declares dependency providers in endpoint defaults.
# ruff: noqa: B008

from __future__ import annotations

import glob
import threading
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import JSONResponse

from law_agent.config import RerankMode
from law_agent.review.case_store import UserRecord
from law_agent.review.evalset.cases import EvalSuite
from law_agent.review.evalset.runner import ReviewEvalMode, run_evaluation
from law_agent.review.evalset.schemas import EvalSummary
from law_agent.review.http.schemas import EvalJobResponse, EvalRunRequest
from law_agent.review.ids import utc_now_iso


def idle_eval_job() -> dict[str, Any]:
    return {
        "job_id": None,
        "status": "idle",
        "message": None,
        "started_at": None,
        "finished_at": None,
    }


def preload_eval_cache(app: FastAPI, cache_dir: Path) -> None:
    patterns = {
        "off": ["*rerank_off*", "*rerank=off*", "*rerank-off*"],
        "embedding": ["*rerank_on*", "*rerank=on*", "*rerank-on*"],
    }
    for arm, names in patterns.items():
        candidates: list[Path] = []
        for name in names:
            candidates.extend(Path(path) for path in glob.glob(str(cache_dir / name)))
        if not candidates:
            continue
        latest = max(candidates, key=lambda path: path.stat().st_mtime)
        try:
            app.state.eval_cache[arm] = EvalSummary.model_validate_json(
                latest.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue


def register_evaluation_routes(
    app: FastAPI,
    *,
    current_user: Callable[..., Any],
    admin_only: Callable[[UserRecord], None],
) -> None:
    router = APIRouter()

    @router.get("/api/eval/latest")
    async def get_latest_eval(
        rerank_mode: RerankMode = "off",
        user: UserRecord = Depends(current_user),
    ) -> JSONResponse:
        admin_only(user)
        if app.state.eval_cache_dir is not None:
            preload_eval_cache(app, app.state.eval_cache_dir)
        cached = app.state.eval_cache.get(rerank_mode)
        if cached is None:
            raise HTTPException(
                status_code=404,
                detail=f"no evaluation has been run for rerank_mode={rerank_mode}",
            )
        return JSONResponse(content=cached.model_dump())

    @router.post("/api/eval/run")
    async def trigger_eval(
        request: EvalRunRequest | None = None,
        user: UserRecord = Depends(current_user),
    ) -> EvalJobResponse:
        admin_only(user)
        chunks = (
            Path(request.chunks_path)
            if request and request.chunks_path
            else app.state.chunks_path
        )
        review_mode_value: ReviewEvalMode = request.review_mode if request else "llm"
        top_k = request.top_k if request else 10
        max_workers = request.max_workers if request else 4
        rerank_mode_value: RerankMode = request.rerank_mode if request else "off"
        suite: EvalSuite = request.suite if request else "full"
        with app.state.eval_lock:
            job = app.state.eval_jobs[rerank_mode_value]
            if job["status"] == "running":
                return EvalJobResponse.model_validate(job)
            job_id = uuid.uuid4().hex
            app.state.eval_jobs[rerank_mode_value] = {
                "job_id": job_id,
                "status": "running",
                "message": None,
                "started_at": utc_now_iso(),
                "finished_at": None,
            }
        thread = threading.Thread(
            target=run_eval_job,
            args=(
                app,
                job_id,
                chunks,
                review_mode_value,
                top_k,
                max_workers,
                rerank_mode_value,
                suite,
            ),
            daemon=True,
        )
        thread.start()
        return EvalJobResponse.model_validate(app.state.eval_jobs[rerank_mode_value])

    @router.get("/api/eval/status")
    async def get_eval_status(
        rerank_mode: RerankMode = "off",
        user: UserRecord = Depends(current_user),
    ) -> EvalJobResponse:
        admin_only(user)
        return EvalJobResponse.model_validate(
            app.state.eval_jobs.get(rerank_mode, idle_eval_job())
        )

    app.include_router(router)


def run_eval_job(
    app: FastAPI,
    job_id: str,
    chunks: Path,
    review_mode: ReviewEvalMode,
    top_k: int,
    max_workers: int,
    rerank_mode: RerankMode,
    suite: EvalSuite,
) -> None:
    try:
        summary = run_evaluation(
            chunks_path=chunks,
            review_mode=review_mode,
            top_k=top_k,
            rerank_mode=rerank_mode,
            max_workers=max_workers,
            suite=suite,
        )
    except Exception as exc:  # noqa: BLE001 - persist evaluation failure
        with app.state.eval_lock:
            job = app.state.eval_jobs.get(rerank_mode, idle_eval_job())
            if job.get("job_id") == job_id:
                app.state.eval_jobs[rerank_mode] = {
                    **job,
                    "status": "failed",
                    "message": str(exc),
                    "finished_at": utc_now_iso(),
                }
        return
    with app.state.eval_lock:
        job = app.state.eval_jobs.get(rerank_mode, idle_eval_job())
        if job.get("job_id") != job_id:
            return
        app.state.eval_cache[rerank_mode] = summary
        app.state.eval_jobs[rerank_mode] = {
            **job,
            "status": "succeeded",
            "message": None,
            "finished_at": utc_now_iso(),
        }


__all__ = ["idle_eval_job", "preload_eval_cache", "register_evaluation_routes"]
