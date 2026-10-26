"""Matter conversation API; only conversation records may be written."""

# ruff: noqa: B008
import json
import logging
import queue
import threading
import time

import anyio
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from starlette.responses import StreamingResponse

from law_agent.review.http.schemas import MatterQuestionRequest
from law_agent.review.llm import ReviewWorkflowFailed
from law_agent.review.matter_agent import answer_matter, answer_matter_stream, matter_context
from law_agent.review.transactions import case_transaction

logger = logging.getLogger(__name__)


def register_matter_routes(app, *, current_user, store, enterprise, can_view):
    router = APIRouter()

    def visible(case_id, user):
        case = store().get_case(case_id)
        if case is None or not can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")

    def save_turn(case_id, user, payload, context, reply):
        with case_transaction(store(), enterprise(), case_id):
            visible(case_id, user)
            matter_context(store(), enterprise(), case_id, payload)
            event = store().add_event(case_id, user.id, event_type="matter_agent_turn", payload={
                "task_id": context["task_id"], "material_snapshot_id": context["material_snapshot_id"],
                "intake_snapshot_id": context["intake_snapshot_id"], "question": payload.question,
                "selection": context.get("selection"),
                "input_provenance": "applicant_statement" if user.role == "requester" else "reviewer_instruction",
                "reply": reply,
            })
        return {"id": event["id"], "created_at": event["created_at"], **event["payload"]}

    @router.get("/api/cases/{case_id}/conversation")
    async def history(case_id: str, user=Depends(current_user)):
        visible(case_id, user)
        return {"items": [
            {"id": e["id"], "created_at": e["created_at"], **e["payload"]}
            for e in store().list_events(case_id) if e["event_type"] == "matter_agent_turn"
        ]}

    @router.post("/api/cases/{case_id}/conversation")
    async def ask(case_id: str, payload: MatterQuestionRequest, user=Depends(current_user)):
        visible(case_id, user)
        if not payload.question.strip():
            raise HTTPException(status_code=422, detail="请输入问题")
        try:
            context = matter_context(store(), enterprise(), case_id, payload)
            reply = await run_in_threadpool(app.state.matter_answerer, payload.question, context)
            return save_turn(case_id, user, payload, context, reply)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ReviewWorkflowFailed, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail="Agent 暂时无法回答，请稍后重试") from exc

    @router.post("/api/cases/{case_id}/conversation/stream")
    async def ask_stream(case_id: str, payload: MatterQuestionRequest, request: Request, user=Depends(current_user)):
        """流式回答：answer 正文逐段 SSE 推送，确定性清洗后一次性下发完整 turn。"""
        visible(case_id, user)
        if not payload.question.strip():
            raise HTTPException(status_code=422, detail="请输入问题")
        try:
            context = matter_context(store(), enterprise(), case_id, payload)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        async def gen():
            events: queue.Queue = queue.Queue()
            stopped = threading.Event()
            deadline = time.monotonic() + app.state.matter_stream_timeout

            def push(event):
                if stopped.is_set():
                    raise RuntimeError("Matter stream closed")
                events.put(event)

            def run_task():
                try:
                    reply = app.state.matter_stream_answerer(payload.question, context, on_event=push)
                    if not stopped.is_set():
                        events.put({"type": "reply", "reply": reply})
                except ValueError as exc:
                    events.put({"type": "error", "message": str(exc)})
                except Exception:
                    logger.exception("Matter answer stream failed")
                    events.put({"type": "error", "message": "暂时无法回答，请重试"})

            async def worker():
                await anyio.to_thread.run_sync(run_task, abandon_on_cancel=True)

            def pull():
                try:
                    return events.get(timeout=0.1)
                except queue.Empty:
                    return None

            try:
                async with anyio.create_task_group() as tasks:
                    tasks.start_soon(worker)
                    yield 'data: {"type":"status","message":"正在整理本案上下文…"}\n\n'
                    while True:
                        if await request.is_disconnected():
                            break
                        if time.monotonic() > deadline:
                            evt = {"type": "error", "message": "回答超时，请重试"}
                        else:
                            evt = await anyio.to_thread.run_sync(pull)
                        if await request.is_disconnected():
                            break
                        if time.monotonic() > deadline:
                            evt = {"type": "error", "message": "回答超时，请重试"}
                        if evt is None:
                            yield ": keepalive\n\n"
                            continue
                        if evt["type"] == "reply":
                            try:
                                turn = await run_in_threadpool(save_turn, case_id, user, payload, context, evt["reply"])
                                evt = {"type": "done", "turn": turn}
                            except (ValueError, HTTPException):
                                evt = {"type": "error", "message": "案件版本或访问权限已变化，请刷新后重试"}
                            except Exception:
                                logger.exception("Matter answer could not be saved")
                                evt = {"type": "error", "message": "回答未能保存，请重试"}
                        yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
                        if evt["type"] in {"done", "error"}:
                            break
                    stopped.set()
                    tasks.cancel_scope.cancel()
            finally:
                stopped.set()

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    app.state.matter_answerer = lambda question, context: answer_matter(
        question, context, chunks_path=app.state.chunks_path,
    )
    app.state.matter_stream_answerer = lambda question, context, **kwargs: answer_matter_stream(
        question, context, chunks_path=app.state.chunks_path, **kwargs,
    )
    app.state.matter_stream_timeout = 300
    app.include_router(router)
