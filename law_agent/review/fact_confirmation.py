"""Explicit applicant confirmation replaces frozen facts, never a running report."""

import os
from dataclasses import asdict
from uuid import uuid4

from law_agent.config import load_llm_config
from law_agent.review.agent import AgentState
from law_agent.review.fact_provenance import append_fact
from law_agent.review.http.schemas import IntakePayload
from law_agent.review.schemas import FactLedgerEntry
from law_agent.review.transactions import case_transaction


def queue_review(*, cases, enterprise, case, user, material_snapshot, intake_snapshot):
    versions = [enterprise.get_material_version(v) for v in material_snapshot.version_ids]
    if any(v is None or v.parse_status != "ready" or not (v.parsed_text or "").strip() for v in versions):
        raise ValueError("快照中存在尚未完成通用解析的材料")
    config = load_llm_config()
    task = enterprise.enqueue_review_task(
        case_id=case["id"], material_snapshot_id=material_snapshot.id,
        intake_snapshot_id=intake_snapshot.id, model_id=config.model or "not-configured",
        data_boundary_summary={
            "base_url": config.base_url,
            "deployment": os.getenv("CROSSCOMPLY_MODEL_BOUNDARY", "enterprise-approved-api"),
        },
    )
    if task.status not in {"queued", "running", "waiting_input"}:
        raise ValueError("当前冻结输入已有终态任务；请更新事实后重新运行")
    cases.update_case(case["id"], status="review_running")
    cases.add_event(
        case["id"], user.id, event_type="review_queued", from_status=case["status"],
        to_status="review_running", payload={"task_id": task.id, "material_snapshot_id": material_snapshot.id},
    )
    return {"task_id": task.id, "status": task.status}


def _current(cases, enterprise, user, case_id, task_id, material_id, intake_id):
    case = cases.get_case(case_id)
    if case is None or user.role != "requester" or (case.get("owner_id") or case["created_by"]) != user.id:
        raise PermissionError("案件不存在或无权确认事实")
    task = enterprise.get_latest_task(case_id)
    material = enterprise.get_latest_material_snapshot(case_id)
    intake = enterprise.get_latest_intake_snapshot(case_id=case_id, material_snapshot_id=material_id)
    if (
        task is None or task.id != task_id or material is None or material.id != material_id
        or intake is None or intake.id != intake_id or task.intake_snapshot_id != intake_id
        or task.material_snapshot_id != material_id
    ):
        raise ValueError("材料、事实或审查版本已变化，请刷新案件")
    if case["status"] != "needs_info":
        raise ValueError("只有等待补充事实的案件可以确认并重新审查")
    return case, task, material, intake


def stage_fact_answer(*, cases, enterprise, user, case_id, payload):
    with case_transaction(cases, enterprise, case_id):
        _case, task, _material, intake = _current(
            cases, enterprise, user, case_id, payload.task_id,
            payload.material_snapshot_id, payload.intake_snapshot_id,
        )
        if task.status != "waiting_input" or task.agent_state is None:
            raise ValueError("当前审查没有等待回答的问题")
        state = AgentState.model_validate(task.agent_state)
        asked = {q.field for q in state.fact_questions}
        if asked and not set(payload.values) <= asked:
            raise ValueError("回答包含本次未询问的事实字段")
        validated = IntakePayload.model_validate({**intake.intake, **payload.values}, strict=True)
        values = {key: validated.model_dump(mode="json")[key] for key in payload.values}
        if state.gate_id != payload.gate_id or not payload.answer.strip():
            raise ValueError("该问题已变化或补充说明为空")
        append_fact(state.fact_ledger, FactLedgerEntry(
            field="supplemental_statement", value=payload.answer.strip(),
            source_type="applicant_statement", source_ref=payload.gate_id, status="unverified",
        ))
        for field, value in values.items():
            append_fact(state.fact_ledger, FactLedgerEntry(
                field=field, value=value, source_type="applicant_statement",
                source_ref=payload.gate_id, status="unverified",
            ))
        state.pending_fact_values = values
        state.fact_answer_revision = uuid4().hex
        task = enterprise.record_waiting_input(task.id, state=state.model_dump(mode="json"), gate_id=payload.gate_id)
        cases.add_event(case_id, user.id, event_type="fact_answer_recorded", payload={
            "task_id": task.id, "gate_id": payload.gate_id, "fields": list(values),
        })
        return asdict(task)


def confirm_facts(*, cases, enterprise, user, case_id, payload):
    with case_transaction(cases, enterprise, case_id):
        case, task, material, intake = _current(
            cases, enterprise, user, case_id, payload.task_id,
            payload.material_snapshot_id, payload.intake_snapshot_id,
        )
        if task.status == "waiting_input":
            state = AgentState.model_validate(task.agent_state)
            if (
                state.gate_id != payload.gate_id or not state.fact_answer_revision
                or state.fact_answer_revision != payload.answer_revision
                or state.pending_fact_values != payload.values
            ):
                raise ValueError("补充回答已变化，请先记录并核对当前事实")
        elif task.status == "succeeded" and payload.conversation_id:
            event = next((e for e in cases.list_events(case_id) if e["id"] == payload.conversation_id), None)
            if (
                not event or event["event_type"] != "matter_agent_turn" or event["actor_id"] != user.id
                or event["payload"].get("input_provenance") != "applicant_statement"
                or event["payload"]["task_id"] != task.id
                or event["payload"]["reply"]["proposed_facts"] != payload.values
            ):
                raise ValueError("对话中的事实建议已过期或与确认内容不同")
        else:
            raise ValueError("审查正在运行或已结束，不能更改冻结事实")
        updated_intake = IntakePayload.model_validate({**intake.intake, **payload.values}, strict=True).model_dump(mode="json")
        if updated_intake == intake.intake:
            raise ValueError("确认内容未改变事实；可直接回答并继续当前审查")
        if IntakePayload.model_validate(case.get("intake") or {}).model_dump(mode="json") != intake.intake:
            raise ValueError("案件填报事实已变化，请刷新并重新冻结")
        snapshot = enterprise.create_intake_snapshot(
            case_id=case_id, material_snapshot_id=material.id,
            intake=updated_intake, created_by=user.id,
            confirmation_ref=intake.id,
        )
        enterprise.supersede_waiting_tasks(case_id)
        cases.update_case(case_id, intake_json=updated_intake, facts_confirmed=True)
        queued = queue_review(
            cases=cases, enterprise=enterprise, case=case, user=user,
            material_snapshot=material, intake_snapshot=snapshot,
        )
        cases.add_event(case_id, user.id, event_type="facts_confirmed_for_review", payload={
            "previous_task_id": task.id, "task_id": queued["task_id"],
            "material_snapshot_id": material.id, "intake_snapshot_id": snapshot.id,
            "previous_intake_snapshot_id": intake.id, "fields": list(payload.values),
        })
        return {**queued, "intake_snapshot": asdict(snapshot)}
