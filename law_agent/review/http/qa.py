"""Owner-scoped compliance Q&A, live answers and explicit case handoff."""

# ruff: noqa: B008

import asyncio
import json
from dataclasses import asdict
from io import BytesIO

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import Field, field_validator
from starlette.concurrency import run_in_threadpool

from law_agent.data.schemas import StrictModel
from law_agent.review.http.cases import material_from_upload
from law_agent.review.http.schemas import CaseCreateRequest
from law_agent.review.qa_store import QAConflict, identifier, now


class MessageRequest(StrictModel):
    question: str = Field(min_length=1, max_length=20000)
    material_ids: list[str] = Field(default_factory=list, max_length=30)
    request_id: str = Field(min_length=1, max_length=100)

    @field_validator("question")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("请输入问题")
        return value.strip()


class CaseHandoffRequest(CaseCreateRequest):
    run_id: str
    request_id: str = Field(min_length=1, max_length=100)
    material_ids: list[str] = Field(default_factory=list, max_length=30)


def public_run(run):
    return {key: run[key] for key in ("id", "question", "material_ids", "status", "answer", "result", "error", "progress", "created_at")}


def public_material(material):
    return {key: material[key] for key in ("id", "filename", "byte_size", "created_at")}


def public_conversation(row):
    return {**{key: row[key] for key in ("id", "title", "created_at", "updated_at")},
            "materials": [public_material(m) for m in row["materials"]], "runs": [public_run(r) for r in row["runs"]]}


_SAFE_DOWNLOAD_TYPES = {
    "application/pdf", "application/json", "application/octet-stream",
    "text/plain", "text/markdown", "text/csv",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _download_media_type(content_type: str | None) -> str:
    """上传内容类型不可信：非白名单一律按二进制流下载。"""

    base = (content_type or "application/octet-stream").split(";")[0].strip().lower()
    return base if base in _SAFE_DOWNLOAD_TYPES else "application/octet-stream"


def register_qa_routes(app, *, current_user, cases, enterprise, originals, case_payload):
    router = APIRouter(prefix="/api/qa/conversations")

    def visible(conversation_id, user):
        row = app.state.qa_store.get(conversation_id)
        if row is None or row["owner_id"] != user.id:
            raise HTTPException(404, "问答不存在或无权访问")
        return row

    def find_run(row, run_id):
        run = next((r for r in row["runs"] if r["id"] == run_id), None)
        if run is None:
            raise HTTPException(404, "回答不存在")
        return run

    @router.get("")
    def history(user=Depends(current_user)):
        return {"items": [{k: row[k] for k in ("id", "title", "created_at", "updated_at")}
                          for row in app.state.qa_store.list(user.id)]}

    @router.post("")
    def create(user=Depends(current_user)):
        return public_conversation(app.state.qa_store.create(user.id))

    @router.get("/{conversation_id}")
    def detail(conversation_id: str, user=Depends(current_user)):
        return public_conversation(visible(conversation_id, user))

    @router.post("/{conversation_id}/materials")
    async def upload(conversation_id: str, file: UploadFile = File(...), user=Depends(current_user)):
        visible(conversation_id, user)
        text, filename = await material_from_upload(file)
        await file.seek(0)
        content = await file.read()
        material_id = identifier("qam")
        stored = await run_in_threadpool(originals().put_original, case_id=conversation_id, logical_name=material_id,
                                        filename=filename, content_type=file.content_type or "application/octet-stream", content=content)
        material = {"id": material_id, "filename": filename, "content_type": file.content_type or "application/octet-stream",
                    "parsed_text": text, "object_key": stored.object_key, "sha256": stored.sha256,
                    "byte_size": stored.byte_size, "created_at": now()}
        return public_material(app.state.qa_store.add_material(conversation_id, material))

    @router.get("/{conversation_id}/materials/{material_id}/original")
    def download(conversation_id: str, material_id: str, user=Depends(current_user)):
        row = visible(conversation_id, user)
        material = next((m for m in row["materials"] if m["id"] == material_id), None)
        if material is None:
            raise HTTPException(404, "材料不存在")
        return StreamingResponse(
            BytesIO(originals().get_original(material["object_key"])),
            media_type=_download_media_type(material["content_type"]),
            headers={
                "Content-Disposition": 'attachment; filename="download"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @router.post("/{conversation_id}/messages")
    def send(conversation_id: str, payload: MessageRequest, user=Depends(current_user)):
        visible(conversation_id, user)
        try:
            return public_run(app.state.qa_store.enqueue(conversation_id, **payload.model_dump()))
        except QAConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/{conversation_id}/runs/{run_id}")
    def status(conversation_id: str, run_id: str, user=Depends(current_user)):
        return public_run(find_run(visible(conversation_id, user), run_id))

    @router.post("/{conversation_id}/runs/{run_id}/retry")
    def retry(conversation_id: str, run_id: str, user=Depends(current_user)):
        find_run(visible(conversation_id, user), run_id)
        try:
            return public_run(app.state.qa_store.retry(conversation_id, run_id))
        except QAConflict as exc:
            raise HTTPException(409, str(exc)) from exc

    @router.get("/{conversation_id}/runs/{run_id}/events")
    async def events(conversation_id: str, run_id: str, request: Request, user=Depends(current_user)):
        find_run(await run_in_threadpool(visible, conversation_id, user), run_id)

        async def stream():
            previous = None
            ticks = 0
            while not await request.is_disconnected():
                row = await run_in_threadpool(visible, conversation_id, user)
                run = public_run(find_run(row, run_id))
                data = json.dumps(run, ensure_ascii=False)
                if data != previous:
                    yield f"event: snapshot\ndata: {data}\n\n"
                    previous = data
                elif ticks % 30 == 0:
                    yield ": keepalive\n\n"
                if run["status"] in {"completed", "failed"}:
                    return
                ticks += 1
                await asyncio.sleep(0.3)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @router.post("/{conversation_id}/cases")
    def handoff(conversation_id: str, payload: CaseHandoffRequest, user=Depends(current_user)):
        visible(conversation_id, user)

        def create_case(row):
            run = find_run(row, payload.run_id)
            if (run["result"] or {}).get("questions"):
                raise HTTPException(409, "请先补充业务事实并完成问答后再发起审查")
            if run["status"] != "completed":
                raise HTTPException(409, "请等待回答完成后再发起审查")
            if not set(payload.material_ids) <= set(run["material_ids"]):
                raise HTTPException(422, "只能转入本轮使用的材料")
            key = payload.request_id
            prior = row["case_links"].get(key)
            signature = payload.model_dump(mode="json")
            if prior:
                if prior["input"] != signature:
                    raise HTTPException(409, "重复请求的内容不一致")
                case = cases().get_case(prior["case_id"])
                return {**case_payload(case, user), "imported_versions": prior["versions"]}
            selected = [m for m in row["materials"] if m["id"] in payload.material_ids]
            case = cases().create_case(**payload.model_dump(exclude={"run_id", "request_id", "material_ids"}),
                                       created_by=user.id, owner_id=user.id)
            versions = []
            for material in selected:
                stored = originals().put_original(case_id=case["id"], logical_name=material["id"], filename=material["filename"],
                                                  content_type=material["content_type"], content=originals().get_original(material["object_key"]))
                version = enterprise().create_material_version(
                    case_id=case["id"], logical_name=material["id"], filename=material["filename"], content_type=material["content_type"],
                    object_key=stored.object_key, sha256=stored.sha256, byte_size=stored.byte_size,
                    uploaded_by=user.id, parse_status="ready", parsed_text=material["parsed_text"], parser="law_agent.review.materials",
                )
                versions.append(asdict(version))
            cases().add_event(case["id"], user.id, event_type="case_created", to_status="draft")
            cases().add_event(case["id"], user.id, event_type="qa_handoff", payload={
                "conversation_id": conversation_id, "run_id": run["id"], "material_ids": payload.material_ids,
                "investigation_leads": run["result"], "requires_formal_review": True,
            })
            row["case_links"][key] = {"case_id": case["id"], "versions": versions, "input": signature}
            return {**case_payload(case, user), "imported_versions": versions}

        return app.state.qa_store._edit(conversation_id, create_case)

    app.include_router(router)
