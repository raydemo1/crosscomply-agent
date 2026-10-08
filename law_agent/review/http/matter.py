"""Matter conversation API; only conversation records may be written."""

# ruff: noqa: B008
from fastapi import APIRouter, Depends, HTTPException
from starlette.concurrency import run_in_threadpool

from law_agent.review.http.schemas import MatterQuestionRequest
from law_agent.review.llm import ReviewWorkflowFailed
from law_agent.review.matter_agent import answer_matter, matter_context
from law_agent.review.semantic_grounding import SemanticGroundingRejected
from law_agent.review.transactions import case_transaction


def register_matter_routes(app, *, current_user, store, enterprise, can_view):
    router = APIRouter()

    def visible(case_id, user):
        case = store().get_case(case_id)
        if case is None or not can_view(user, case):
            raise HTTPException(status_code=404, detail="案件不存在或无权访问")

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
            with case_transaction(store(), enterprise(), case_id):
                visible(case_id, user)
                matter_context(store(), enterprise(), case_id, payload)
                event = store().add_event(case_id, user.id, event_type="matter_agent_turn", payload={
                    "task_id": context["task_id"], "material_snapshot_id": context["material_snapshot_id"],
                    "intake_snapshot_id": context["intake_snapshot_id"], "question": payload.question,
                    "input_provenance": "applicant_statement" if user.role == "requester" else "reviewer_instruction",
                    "reply": reply,
                })
            return {"id": event["id"], "created_at": event["created_at"], **event["payload"]}
        except SemanticGroundingRejected as exc:
            raise HTTPException(status_code=422, detail="回答未通过证据核验，请缩小问题范围或补充事实") from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (ReviewWorkflowFailed, RuntimeError) as exc:
            raise HTTPException(status_code=503, detail="Agent 暂时无法回答，请稍后重试") from exc

    app.state.matter_answerer = lambda question, context: answer_matter(
        question, context, chunks_path=app.state.chunks_path,
    )
    app.include_router(router)
