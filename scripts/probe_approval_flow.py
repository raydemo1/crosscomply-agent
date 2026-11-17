"""Exercise real API routes, ReviewWorker, and an isolated PostgreSQL database."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import psycopg
from alembic.config import Config
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from run_agent_baseline import write_json
from sqlalchemy import URL

from alembic import command
from law_agent.config import load_service_config
from law_agent.kb.enrichment import PostgresEnrichmentStore
from law_agent.llm.usage import usage_scope
from law_agent.review.agent import AgentState
from law_agent.review.api import create_app
from law_agent.review.db_pool import close_pool
from law_agent.review.fact_provenance import confirmed_intake_ledger
from law_agent.review.feishu import ApprovalInstance, FeishuApprovalConfig
from law_agent.review.object_store import material_object_store_from_env
from law_agent.review.schemas import EvidenceSelfCheck, ReviewFacts
from law_agent.review.worker import build_production_review_worker


class LocalApprovalProvider:
    """Capture outbound approval form locally. Never contacts a Feishu tenant."""

    def __init__(self):
        self.requests = []

    def create_instance(self, **kwargs):
        self.requests.append(kwargs)
        return ApprovalInstance("local-approval-instance", "local-provider-request")


def check(response, expected):
    assert response.status_code == expected, (response.status_code, response.text)
    return response.json()


def login(client, role):
    check(client.post("/api/auth/login", json={"username": f"{role}@pilot.example", "password": "pilot-test-password"}), 200)


def report_step(root, records, name, data):
    records[name] = data
    write_json(root / "flow.json", records)
    print(json.dumps({"step": name}, ensure_ascii=False), flush=True)


def run_flow(client, worker, provider, root, *, mode):
    records = {"mode": mode}
    requests = []
    material = (
        "合成审查材料：境内企业的模拟客服档案仅存在中国大陆服务器。"
        "所有操作员均在中国大陆。资料送审时尚未核对有无向境外提供数据的渠道或权限。"
    )
    goal = (
        "只判断本业务是否向境外提供数据。该事实未确认时，请先使用结构化追问字段 "
        "cross_border_transfer 等待业务申报人确认。已有正式确认时直接以该版本事实核对，只回答是否出境。"
    )
    login(client, "requester")
    case = check(client.post("/api/cases", json={"question": goal, "material_text": material}), 200)["case"]
    case_id = case["id"]
    path = f"/api/cases/{case_id}"
    version = check(client.post(path + "/materials", data={"logical_name": "业务材料"},
                                files={"file": ("material.txt", material.encode(), "text/plain")}), 200)
    check(client.post(path + "/material-snapshots", json={"version_ids": [version["id"]]}), 200)
    check(client.post(path + "/status", json={"status": "pending_review"}), 200)
    with usage_scope("workflow_first_review", requests.append):
        waiting = worker.run_once()
    write_json(root / "requests.json", requests)
    paused = check(client.get(path), 200)
    report_step(root, records, "before_confirmation", paused)
    assert waiting and waiting.status == "waiting_input", paused["approval_readiness"]
    assert paused["approval_readiness"]["code"] == "fact_review"
    login(client, "reviewer")
    blocked = check(client.post(path + "/feishu-approval"), 409)
    assert not provider.requests
    report_step(root, records, "approval_blocked", blocked)

    login(client, "requester")
    answer = {
        "task_id": waiting.id, "material_snapshot_id": waiting.material_snapshot_id,
        "intake_snapshot_id": waiting.intake_snapshot_id, "gate_id": waiting.agent_state["gate_id"],
        "answer": "已核对访问权限和访问日志。境外主体无法访问，本业务从未把数据提供给境外。",
        "values": {"cross_border_transfer": False},
    }
    staged = check(client.post(path + "/fact-answers", json=answer), 200)
    confirmation = {key: value for key, value in answer.items() if key != "answer"}
    confirmation.update(confirmed=True, answer_revision=staged["agent_state"]["fact_answer_revision"])
    confirmed = check(client.post(path + "/confirm-facts", json=confirmation), 202)
    report_step(root, records, "confirmed", confirmed)
    assert confirmed["intake_snapshot"]["id"] != waiting.intake_snapshot_id
    stale = check(client.post(path + "/confirm-facts", json=confirmation), 409)
    report_step(root, records, "stale_confirmation_blocked", stale)
    with usage_scope("workflow_resumed_review", requests.append):
        reviewed = worker.run_once()
    write_json(root / "requests.json", requests)
    reviewed_case = check(client.get(path), 200)
    report_step(root, records, "after_review", reviewed_case)
    assert reviewed and reviewed.status == "succeeded", reviewed_case["approval_readiness"]
    assert reviewed_case["approval_readiness"]["code"] == "ready", reviewed_case["approval_readiness"]

    login(client, "reviewer")
    approval = check(client.post(path + "/feishu-approval"), 200)
    assert approval["task_id"] == reviewed.id
    assert check(client.get(path), 200)["approval_readiness"]["code"] == "submitted"
    assert check(client.post(path + "/feishu-approval"), 200)["id"] == approval["id"]
    assert len(provider.requests) == 1
    report_step(root, records, "outbound_local_provider", {"approval": approval, "calls": len(provider.requests)})
    body = json.dumps(
        {"token": "local-token", "header": {"event_id": "approval-local-1", "event_type": "approval_instance"},
         "event": {"instance_code": approval["instance_id"], "status": "APPROVED",
                   "approval_time": "2026-11-16T12:00:00+08:00", "approver_id": "pilot-reviewer"}},
        separators=(",", ":"),
    ).encode()
    headers = {"content-type": "application/json", "x-lark-request-timestamp": "123",
               "x-lark-request-nonce": "local", "x-lark-signature": hashlib.sha256(b"123locallocal-encrypt" + body).hexdigest()}
    callback = "/api/integrations/feishu/approval-events"
    bad_signature = check(client.post(callback, content=body, headers={**headers, "x-lark-signature": "invalid"}), 401)
    first = check(client.post(callback, content=body, headers=headers), 200)
    repeat = check(client.post(callback, content=body, headers=headers), 200)
    terminal = check(client.get(path), 200)
    assert first["case_status"] == repeat["case_status"] == terminal["case"]["status"] == "approved"
    assert not first["duplicate"] and repeat["duplicate"]
    assert terminal["approval_readiness"]["code"] == "decided"
    decision_writes = sum(event["event_type"] == "feishu_decision_written_back" for event in terminal["events"])
    assert decision_writes == 1
    report_step(root, records, "callback", {"bad_signature": bad_signature, "first": first, "repeat": repeat,
                                            "decision_writes": decision_writes, "terminal": terminal})


def replay_runtime(path):
    actual = json.loads(path.read_text(encoding="utf-8"))
    previous_pause = AgentState.model_validate(actual["before_confirmation"]["review_task"]["agent_state"])

    def execute(task, *, store, goal, **_kwargs):
        snapshot = store.get_intake_snapshot(task.intake_snapshot_id)
        ledger = confirmed_intake_ledger(snapshot.intake, snapshot.id)
        confirmed = snapshot.intake.get("cross_border_transfer")
        if confirmed is None:
            return previous_pause.model_copy(update={"goal": goal, "fact_ledger": ledger})
        assert confirmed is False
        facts = ReviewFacts(cross_border_transfer=False)
        summary = "申报人已确认本业务未向境外提供数据。本次送审回放仅以该合成事实验证审批状态与归档。"
        trace_id = f"replay-{task.id}"
        payload = {
            "review_case_id": task.case_id, "trace_id": trace_id,
            "review_facts": facts.model_dump(mode="json"), "citation_groups": [],
            "evidence_self_check": EvidenceSelfCheck(status="sufficient").model_dump(mode="json"),
            "semantic_grounding": {"status": "supported", "fixture": "deterministic_approval_replay"},
            "review_result": {
                "review_result_id": trace_id, "review_case_id": task.case_id, "trace_id": trace_id,
                "risk_level": "low", "decision_summary": summary, "legal_path": None, "conclusion": summary,
                "trigger_reasons": [], "missing_information": [], "legal_basis_gaps": [],
                "recommended_actions": [], "risk_boundaries": [], "claims": [],
                "citations": [], "applicable_evidence": [], "issues": [],
            },
            "fact_ledger": [entry.model_dump(mode="json") for entry in ledger],
        }
        return AgentState(goal=goal, status="completed", facts=facts, fact_ledger=ledger, result=payload)
    return execute


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--replay-from", type=Path, help="Use a recorded pause and synthetic completion to test the workflow without model calls")
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    dsn = load_service_config().postgres.dsn
    database = "crosscomply_pilot_" + uuid4().hex[:12]
    isolated_dsn = make_conninfo(dsn, dbname=database)
    connection_options = conninfo_to_dict(isolated_dsn)
    migration_url = URL.create(
        "postgresql+psycopg", username=connection_options.get("user"), password=connection_options.get("password"),
        host=connection_options.get("host"), port=int(connection_options.get("port", 5432)), database=database,
    )
    provider = LocalApprovalProvider()
    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        try:
            with patch.dict(os.environ, {"PG_DSN": migration_url.render_as_string(hide_password=False).replace("%", "%%")}):
                command.upgrade(Config("alembic.ini"), "head")
            with patch.dict(os.environ, {"PG_DSN": isolated_dsn, "CROSSCOMPLY_MINIO_BUCKET": database.replace("_", "-")}):
                app = create_app(material_object_store=material_object_store_from_env(), feishu_client=provider,
                                 knowledge_corpus=args.output_dir / "knowledge",
                                 feishu_config=FeishuApprovalConfig("local", "local", "local", "local-token",
                                                                   "local-encrypt", "pilot", "http://localhost"))
            with TestClient(app) as client:
                for role in ("requester", "reviewer"):
                    app.state.user_admin_store.create_user(f"{role}@pilot.example", role, "pilot-test-password", role)
                runtime = (patch("law_agent.review.agent_runtime.execute_agent_task", side_effect=replay_runtime(args.replay_from))
                           if args.replay_from else nullcontext())
                with runtime:
                    worker = build_production_review_worker(
                        case_store=app.state.case_store, queue=app.state.enterprise_store,
                        enrichment_store=PostgresEnrichmentStore(isolated_dsn), chunks_path=app.state.chunks_path,
                        worker_id="approval-pilot",
                    )
                    run_flow(client, worker, provider, args.output_dir, mode="replay" if args.replay_from else "live")
        finally:
            close_pool(isolated_dsn)
            connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))


if __name__ == "__main__":
    main()
