"""FastAPI application for the local review API (Issue 10).

Exposes the review flow through a local JSON API so the frontend can call
real review behavior. All business logic stays in ``service.py`` — this
module is a thin transport layer.

Endpoints:
- ``POST /api/review`` — run a full review case
- ``GET /api/eval/latest`` — get the latest cached eval summary
- ``POST /api/eval/run`` — trigger evaluation and cache result
- ``GET /api/health`` — health check
- ``GET /docs`` — OpenAPI interactive docs (provided by FastAPI)
"""

from __future__ import annotations

import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError

from law_agent.config import RerankMode, load_llm_config, load_service_config
from law_agent.review.evalset.cases import EvalSuite
from law_agent.review.evalset.runner import run_evaluation
from law_agent.review.evalset.runner import ReviewEvalMode
from law_agent.review.evalset.schemas import EvalSummary
from law_agent.review.io import read_review_results, read_retrieval_traces
from law_agent.review.llm import ReviewWorkflowFailed
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH
from law_agent.review.schemas import (
    CitationGroup,
    EvidenceSelfCheck,
    ReviewFailedResponse,
    ReviewFacts,
    ReviewResult,
    RetrievalHit,
    RetrievalQuery,
    SourceEvidencePacket,
)
from law_agent.review.service import (
    DEFAULT_REVIEW_RUNS_DIR,
    ReviewMode,
    create_review_case,
    run_service_retrieval,
)

# Upload guardrails. The frontend mirrors these so that network/upload
# failures are intercepted before they can become a "trace" — a parsing
# failure must surface as a clear 422 error, not a ReviewFailedResponse.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
ALLOWED_UPLOAD_SUFFIXES = {
    ".txt", ".md", ".markdown", ".pdf", ".docx",
    ".html", ".htm", ".json",
}


def _file_parse_hint(filename: str, exc: BaseException) -> str:
    """Turn a low-level parsing error into a clear, actionable message.

    File parsing failures must never be reported as a successful-but-failed
    review trace. The user needs to know *why* the document could not be
    parsed so they can fix the input (re-save, OCR, switch format, etc.).
    """
    message = str(exc).strip() or exc.__class__.__name__
    lower = message.lower()
    suffix = Path(filename).suffix.lower()
    if "Docling parser requires" in message or "docling parser requires" in lower:
        return (
            f"无法使用 Docling 解析 {filename}：未安装 docling 或模型文件缺失。"
            "请改用 .txt/.md/.docx 等可解析格式，或安装 docling 后重试。"
        )
    if "MinerU parser" in message or "mineru" in lower:
        return (
            f"无法使用 MinerU 解析 {filename}：未安装 mineru CLI。"
            "请改用其他格式或安装 mineru 后重试。"
        )
    if "non-zip" in lower:
        return (
            f"{filename} 不是有效的 DOCX 文件（ZIP 头校验失败），"
            "可能是旧版 .doc 或损坏文件，请另存为 .docx 后重试。"
        )
    if "转换为 pdf" in message or "转换为 PDF" in message:
        return (
            f"{filename} 无法解析：图片转 PDF 失败（{message}）。"
            "请确认图片未损坏，或直接提供 PDF 文档。"
        )
    if suffix in {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}:
        # Docling could not even load the document — usually a corrupt,
        # empty, or password-protected file (NOT an OCR-quality issue).
        if "could not load document" in lower or "data format error" in lower \
                or "conversion failed" in lower or "is not valid" in lower:
            return (
                f"{filename} 无法加载：文件为空、损坏或受密码保护。"
                "请确认文件可正常打开后重新上传。"
            )
        # OCR ran but produced nothing usable — scanned doc with poor quality.
        if "ocr" in lower:
            return (
                f"{filename} 解析后未提取到有效文本，可能是扫描件且 OCR 识别失败。"
                "请提供可选择文本的文档，或提升扫描清晰度后重试。"
            )
    return f"无法解析文件 {filename}：{message}"


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ReviewRequest(BaseModel):
    """Request body for POST /api/review."""

    question: str = Field(..., min_length=1, description="The review question")
    material_text: str = Field(..., min_length=1, description="The material text to review")
    review_mode: str = Field(default="llm", description="Review pipeline: 'llm' or 'multi_agent'")
    rerank_mode: RerankMode = Field(default="off", description="Rerank arm: 'off' or 'embedding'")


class ReviewResponse(BaseModel):
    """Response body for POST /api/review."""

    review_case_id: str
    trace_id: str
    review_facts: ReviewFacts
    review_result: ReviewResult
    evidence_self_check: EvidenceSelfCheck
    citation_groups: list[CitationGroup] = Field(default_factory=list)
    second_retrieval_triggered: bool = False
    # Issue: 审查工作台产品化 — expose the retrieval query plan and the
    # final evidence hits (with chunk text) so the frontend can render the
    # full review chain and expand citations to show the underlying clause
    # text. These are additive and default to empty for backward compat.
    retrieval_queries: list[RetrievalQuery] = Field(default_factory=list)
    evidence_chunks: list[RetrievalHit] = Field(default_factory=list)
    source_evidence_packets: list[SourceEvidencePacket] = Field(default_factory=list)


class HealthResponse(BaseModel):
    """Response body for GET /api/health."""

    status: str = "ok"
    llm: dict[str, Any] = Field(default_factory=dict)
    services: dict[str, Any] = Field(default_factory=dict)
    corpus: dict[str, Any] = Field(default_factory=dict)


class EvalRunRequest(BaseModel):
    """Request body for POST /api/eval/run (all fields optional)."""

    chunks_path: str | None = Field(default=None, description="Custom path to chunks.jsonl")
    review_mode: ReviewEvalMode = Field(
        default="llm",
        description="Review owner under test: llm or multi_agent",
    )
    top_k: int = Field(default=10, ge=1, le=100, description="Retrieval top_k")
    max_workers: int = Field(
        default=4,
        ge=1,
        le=16,
        description="Number of eval cases to run in parallel",
    )
    rerank_mode: RerankMode = Field(
        default="off",
        description="Optional post-fusion reranker for A/B eval",
    )
    suite: EvalSuite = Field(
        default="full",
        description="Evaluation suite to run: base (24 cases) or full (all cases)",
    )


class EvalJobResponse(BaseModel):
    """Current state of the background evaluation job."""

    job_id: str | None = None
    status: Literal["idle", "running", "succeeded", "failed"] = "idle"
    message: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


def _idle_job() -> dict[str, Any]:
    return {
        "job_id": None,
        "status": "idle",
        "message": None,
        "started_at": None,
        "finished_at": None,
    }


def _preload_eval_cache(app: FastAPI, cache_dir: Path) -> None:
    """Load the most recent ``rerank=off`` and ``rerank=on`` eval summaries
    from ``cache_dir`` into ``app.state.eval_cache``.

    Filenames must contain ``rerank_off`` / ``rerank_on`` (or ``rerank=off`` /
    ``rerank=on``) to be recognized. The most recent file per arm wins.
    """
    import glob

    if not cache_dir.exists():
        return

    patterns = {
        "off": ["*rerank_off*", "*rerank=off*", "*rerank-off*"],
        "embedding": ["*rerank_on*", "*rerank=on*", "*rerank-on*"],
    }
    for arm, pats in patterns.items():
        candidates: list[Path] = []
        for pat in pats:
            candidates.extend(Path(p) for p in glob.glob(str(cache_dir / pat)))
        if not candidates:
            continue
        # Pick the most recently modified file.
        latest = max(candidates, key=lambda p: p.stat().st_mtime)
        try:
            summary = EvalSummary.model_validate_json(latest.read_text(encoding="utf-8"))
            app.state.eval_cache[arm] = summary
        except Exception:
            # Don't fail startup over a stale cache file.
            pass


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    *,
    chunks_path: Path | str = DEFAULT_CHUNKS_PATH,
    review_mode: ReviewMode = "llm",
    eval_cache_dir: Path | str | None = None,
) -> FastAPI:
    """Create and configure the FastAPI application.

    Args:
        chunks_path: Path to the corpus chunks.jsonl file.
        eval_cache_dir: Optional directory with ``eval_full_rerank_off_*.json``
            and ``eval_full_rerank_on_*.json`` files to pre-populate the eval
            cache on startup so the dashboard can show results without a
            fresh run. Files are matched by ``rerank=off`` / ``rerank=on``
            in the filename.
    """

    app = FastAPI(
        title="LawAgent Review API",
        description="Local JSON API for material-driven legal compliance review.",
        version="0.1.0",
    )

    # CORS: allow local frontend dev
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:*", "http://127.0.0.1:*"],
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Store config in app state
    app.state.chunks_path = Path(chunks_path)
    app.state.review_mode = review_mode
    # Per-app eval cache so two app instances never share eval results.
    # Cache is keyed by rerank_mode ("off" / "embedding") so both arms of an
    # A/B eval can coexist without overwriting each other.
    app.state.eval_cache: dict[str, EvalSummary | None] = {"off": None, "embedding": None}
    # eval_jobs is also keyed by rerank_mode so the two arms can run independently.
    app.state.eval_jobs: dict[str, dict[str, Any]] = {
        "off": _idle_job(),
        "embedding": _idle_job(),
    }
    app.state.eval_lock = threading.Lock()
    app.state.eval_cache_dir = Path(eval_cache_dir) if eval_cache_dir is not None else None

    # Pre-populate eval cache from disk so the dashboard can display the
    # latest A/B results without waiting for a fresh run.
    if app.state.eval_cache_dir is not None:
        _preload_eval_cache(app, app.state.eval_cache_dir)

    # ------------------------------------------------------------------
    # Endpoints
    # ------------------------------------------------------------------

    @app.get("/api/health", response_model=HealthResponse)
    async def health_check() -> HealthResponse:
        """Health check endpoint."""

        llm_config = load_llm_config()
        llm_status = {
            "configured": llm_config.enabled,
            "reachable": llm_config.enabled,
            "model": llm_config.model,
            "base_url": llm_config.base_url,
        }

        services: dict[str, Any] = {}
        try:
            from law_agent.review.retrieval.service_backends import healthcheck

            services = healthcheck(load_service_config())
        except Exception as exc:  # noqa: BLE001 - health must be non-fatal
            services = {
                "elasticsearch": False,
                "postgres": False,
                "error": str(exc),
            }

        chunks_path = Path(app.state.chunks_path)
        corpus = {
            "chunks_path": str(chunks_path),
            "chunks_file_exists": chunks_path.exists(),
            "indexed_count": {
                "elasticsearch_docs": services.get("elasticsearch_docs", 0),
                "pgvector_rows": services.get("pgvector_rows", 0),
            },
        }

        status = "ok"
        if not (
            services.get("elasticsearch") and services.get("postgres")
        ):
            status = "degraded"
        if not llm_config.enabled:
            status = "degraded"

        return HealthResponse(
            status=status,
            llm=llm_status,
            services=services,
            corpus=corpus,
        )

    @app.post("/api/review", response_model=ReviewResponse)
    async def run_review(
        request: Request,
        question: str | None = Form(default=None),
        material_text: str = Form(default=""),
        review_mode: str = Form(default=""),
        rerank_mode: str = Form(default="off"),
        file: UploadFile | None = File(default=None),
    ) -> ReviewResponse | JSONResponse:
        """Run a full review case: create case, hybrid retrieval, build result.

        Accepts either:
        - JSON body: ``{"question": "...", "material_text": "..."}``
        - form field: ``question`` + ``material_text``
        - ``file``: uploaded document file (.txt, .md, .pdf, .docx, .html, .json)

        When a file is provided, it is saved to the review run directory and
        text is extracted using the project's docling pipeline. The file
        becomes part of the review case history (MaterialRecord.uploaded_file).

        Returns structured review result with facts, evidence self-check,
        citation groups, and trace IDs.
        """

        if file is None and question is None and _is_json_request(request):
            try:
                payload = ReviewRequest.model_validate(await request.json())
            except ValidationError as exc:
                raise HTTPException(status_code=422, detail=exc.errors())
            question = payload.question
            material_text = payload.material_text
            if not review_mode:
                review_mode = payload.review_mode
            if not rerank_mode or rerank_mode == "off":
                rerank_mode = payload.rerank_mode

        # Resolve review_mode: per-request override > app default
        effective_review_mode = review_mode.strip() if review_mode.strip() else app.state.review_mode

        if question is None:
            raise HTTPException(
                status_code=422,
                detail=[{
                    "type": "missing",
                    "loc": ["body", "question"],
                    "msg": "Field required",
                    "input": None,
                }],
            )

        question = question.strip()
        if not question:
            raise HTTPException(status_code=400, detail="question must not be blank")

        # Use a temp directory for the review run
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)

            try:
                if file is not None and file.filename:
                    # --- File upload mode ---
                    # Guardrails: reject unsupported types / oversized / empty
                    # files BEFORE attempting to parse, so upload mistakes
                    # surface as a clear 422 error instead of a review trace.
                    filename = Path(file.filename).name
                    suffix = Path(filename).suffix.lower()
                    if suffix not in ALLOWED_UPLOAD_SUFFIXES:
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "code": "unsupported_file_type",
                                "filename": filename,
                                "message": (
                                    f"不支持的文件类型 {suffix or '（无后缀）'}。"
                                    "支持：.txt .md .pdf .docx .html .json"
                                ),
                            },
                        )

                    raw = await file.read()
                    if not raw:
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "code": "empty_file",
                                "filename": filename,
                                "message": f"文件 {filename} 为空，无法解析。",
                            },
                        )
                    if len(raw) > MAX_UPLOAD_BYTES:
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "code": "file_too_large",
                                "filename": filename,
                                "size": len(raw),
                                "limit": MAX_UPLOAD_BYTES,
                                "message": (
                                    f"文件 {filename} 大小 {len(raw) / 1024 / 1024:.1f} MB "
                                    f"超过上限 {MAX_UPLOAD_BYTES / 1024 / 1024:.0f} MB。"
                                ),
                            },
                        )

                    # Save the file to the review run directory
                    uploads_dir = tmp_path / "uploads"
                    uploads_dir.mkdir(exist_ok=True)
                    saved_path = uploads_dir / filename
                    saved_path.write_bytes(raw)

                    # Convert file to MaterialRecord (docling extraction).
                    # File parsing failures must surface as a clear 422 error,
                    # NOT as a ReviewFailedResponse "trace" — the user needs
                    # to know the document could not be parsed.
                    from law_agent.review.materials import material_from_file

                    try:
                        material_record = material_from_file(saved_path)
                    except (FileNotFoundError, RuntimeError, ValueError) as exc:
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "code": "file_parse_failed",
                                "filename": filename,
                                "message": _file_parse_hint(filename, exc),
                            },
                        )

                    # Reject empty extraction results (e.g. scanned PDF with
                    # no OCR text) — never pretend a parse failure is success.
                    if not material_record.material_text or not material_record.material_text.strip():
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "code": "empty_extraction",
                                "filename": filename,
                                "message": (
                                    f"{filename} 解析后未提取到任何文本内容，"
                                    "可能是扫描件、图片型 PDF 或加密文档。"
                                    "请提供可选择文本的文档。"
                                ),
                            },
                        )

                    # Create review case with the material record
                    response = create_review_case(
                        question=question,
                        material=material_record,
                        output_dir=tmp_path,
                        review_mode=effective_review_mode,
                    )
                else:
                    # --- Pasted text mode ---
                    if not material_text.strip():
                        raise HTTPException(
                            status_code=400,
                            detail="material_text or file must be provided",
                        )
                    response = create_review_case(
                        question=question,
                        material_text=material_text,
                        output_dir=tmp_path,
                        review_mode=effective_review_mode,
                    )

                case_id = response.review_case.review_case_id
                trace_id = response.trace.trace_id

                trace = run_service_retrieval(
                    case_id=case_id,
                    chunks_path=app.state.chunks_path,
                    output_dir=tmp_path,
                    review_mode=effective_review_mode,
                    rerank_mode=rerank_mode,
                    output_format="markdown",
                )

                # Read the structured result
                results = read_review_results(tmp_path / "review_results.jsonl")
                if not results:
                    raise HTTPException(
                        status_code=500,
                        detail="review result was not generated",
                    )
                review_result = results[0]

                from law_agent.review.service import flatten_source_evidence_packets

                evidence_chunks = flatten_source_evidence_packets(
                    trace.source_evidence_packets
                )

                return ReviewResponse(
                    review_case_id=case_id,
                    trace_id=trace_id,
                    review_facts=response.review_case.review_facts,
                    review_result=review_result,
                    evidence_self_check=trace.evidence_self_check,
                    citation_groups=review_result.applicable_evidence,
                    second_retrieval_triggered=trace.evidence_self_check.second_retrieval_triggered,
                    retrieval_queries=trace.queries,
                    evidence_chunks=evidence_chunks,
                    source_evidence_packets=trace.source_evidence_packets,
                )
            except HTTPException:
                raise
            except ReviewWorkflowFailed as exc:
                failed = ReviewFailedResponse.model_validate(exc.to_response())
                return JSONResponse(status_code=200, content=failed.model_dump())
            except (FileNotFoundError, RuntimeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc))
            except Exception as exc:
                raise HTTPException(
                    status_code=500,
                    detail=f"unexpected error during review: {exc}",
                )

    @app.get("/api/eval/latest")
    async def get_latest_eval(rerank_mode: RerankMode = "off") -> JSONResponse:
        """Get the latest cached evaluation summary for the given rerank arm.

        Returns 404 if no evaluation has been run for this rerank_mode yet.
        """

        if app.state.eval_cache_dir is not None:
            _preload_eval_cache(app, app.state.eval_cache_dir)
        cached = app.state.eval_cache.get(rerank_mode)
        if cached is None:
            raise HTTPException(
                status_code=404,
                detail=f"no evaluation has been run for rerank_mode={rerank_mode}. "
                       "POST /api/eval/run to trigger one.",
            )
        return JSONResponse(content=cached.model_dump())

    @app.post("/api/eval/run")
    async def trigger_eval(request: EvalRunRequest | None = None) -> EvalJobResponse:
        """Start evaluation in the background and return immediately.

        The rerank_mode field selects which A/B arm to populate. The two arms
        (``off`` and ``embedding``) run independently and do not block each
        other, so both can be triggered back-to-back.
        """

        chunks = (
            Path(request.chunks_path) if request and request.chunks_path else app.state.chunks_path
        )
        review_mode = request.review_mode if request else "llm"
        top_k = request.top_k if request else 10
        max_workers = request.max_workers if request else 4
        rerank_mode = request.rerank_mode if request else "off"
        suite = request.suite if request else "full"

        with app.state.eval_lock:
            job = app.state.eval_jobs.get(rerank_mode, _idle_job())
            if job["status"] == "running":
                return EvalJobResponse.model_validate(job)

            job_id = uuid.uuid4().hex
            app.state.eval_jobs[rerank_mode] = {
                "job_id": job_id,
                "status": "running",
                "message": None,
                "started_at": _now_iso(),
                "finished_at": None,
            }

        thread = threading.Thread(
            target=_run_eval_job,
            args=(
                app,
                job_id,
                chunks,
                review_mode,
                top_k,
                max_workers,
                rerank_mode,
                suite,
            ),
            daemon=True,
        )
        thread.start()
        return EvalJobResponse.model_validate(app.state.eval_jobs[rerank_mode])

    @app.get("/api/eval/status")
    async def get_eval_status(rerank_mode: RerankMode = "off") -> EvalJobResponse:
        """Return the current background evaluation job state for the given arm."""

        return EvalJobResponse.model_validate(
            app.state.eval_jobs.get(rerank_mode, _idle_job())
        )

    return app


def _is_json_request(request: Request) -> bool:
    """Return True when the request body is JSON."""

    content_type = request.headers.get("content-type", "").lower()
    return "application/json" in content_type


def _run_eval_job(
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
    except Exception as exc:  # noqa: BLE001 - surfaced through job status
        with app.state.eval_lock:
            job = app.state.eval_jobs.get(rerank_mode, _idle_job())
            if job.get("job_id") == job_id:
                app.state.eval_jobs[rerank_mode] = {
                    **job,
                    "status": "failed",
                    "message": str(exc),
                    "finished_at": _now_iso(),
                }
        return

    with app.state.eval_lock:
        job = app.state.eval_jobs.get(rerank_mode, _idle_job())
        if job.get("job_id") != job_id:
            return
        app.state.eval_cache[rerank_mode] = summary
        app.state.eval_jobs[rerank_mode] = {
            **job,
            "status": "succeeded",
            "message": None,
            "finished_at": _now_iso(),
        }


def _now_iso() -> str:
    from law_agent.review.ids import utc_now_iso

    return utc_now_iso()


# ---------------------------------------------------------------------------
# Default app instance (for uvicorn import)
# ---------------------------------------------------------------------------

_DEFAULT_EVAL_CACHE_DIR = Path("data/review_runs")

app = create_app(eval_cache_dir=_DEFAULT_EVAL_CACHE_DIR)
