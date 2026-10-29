"""Validate bounded retrieval through the native formal-review runtime."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time
import urllib.error
import urllib.request
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from law_agent.config import load_env_file, require_agent_llm_config
from law_agent.llm.usage import UsageRequest, usage_scope
from law_agent.review import agent, agent_tools
from law_agent.review.agent_tools import ComplianceAgentTools
from law_agent.review.enterprise_store import InMemoryEnterpriseStore
from law_agent.review.evalset.agent_runner import (
    StructuredAgentJudge, make_production_executor, run_agent_case,
)
from law_agent.review.evalset import agent_runner
from law_agent.review.evalset.agent_schemas import AgentCase
from law_agent.review.tool_calling import ToolCallingAgent
from scripts.eval_qa_retrieval_chain import (
    FIXTURE, read, read_only_adapters, source_hashes, visible_texts, write,
)

CASES = ["review_customer_transfer_report_001", "review_hr_exemption_pending_001"]


def restore_case(store, case, previous, model):
    from law_agent.review.evalset.agent_runner import build_case_task

    source = read(previous / "model_01.request.json")
    context = next(json.loads(m["content"]) for m in source["messages"] if m["role"] == "user")
    saved = read(previous / "state.json")
    task = build_case_task(store, case, model_id=model)
    snapshot = store.get_material_snapshot(task.material_snapshot_id)
    version = store.get_material_version(snapshot.version_ids[0])
    original_version_id, = context["frozen_material_version_ids"]
    del store.material_versions[version.id]
    version = replace(version, id=original_version_id)
    store.material_versions[version.id] = version
    snapshot = store.create_material_snapshot(case_id=case.case_id, version_ids=[version.id], created_by="agent_eval")
    original_intake = context["confirmed_intake"]
    intake = store.create_intake_snapshot(case_id=case.case_id, material_snapshot_id=snapshot.id,
                                        intake=original_intake["facts"], created_by="agent_eval")
    del store.intake_snapshots[intake.id]
    intake = replace(intake, id=original_intake["id"])
    store.intake_snapshots[intake.id] = intake
    task.material_snapshot_id, task.intake_snapshot_id = snapshot.id, intake.id
    task.agent_state = saved
    assert saved["status"] == "running" and saved["goal"] == case.question
    assert saved["material_preview"]["text"] == f"【材料 {version.logical_name} v{version.version_number} | {version.id}】\n{case.material_text}"
    assert all(row["source_ref"] == intake.id for row in saved["fact_ledger"] if row["status"] == "confirmed")
    return task


def assess(out, cases):
    load_env_file()
    cfg = require_agent_llm_config()
    assert cfg.model == "deepseek-flash"
    rows = []
    for case in cases:
        dest = out / case.case_id
        if not (dest / "delivery.json").exists():
            continue
        if (dest / "judge.json").exists():
            verdict = read(dest / "judge.json")
        else:
            judge = StructuredAgentJudge(on_input=lambda messages: write(
                dest / "judge_input.json", [{"role": m.role, "content": m.content} for m in messages]))
            usage = []
            print("ASSESS_REVIEW", case.case_id, flush=True)
            with usage_scope("formal_review_migration_assessment", usage.append):
                verdict = judge(case, read(dest / "delivery.json"), [], []).model_dump(mode="json")
            write(dest / "judge.json", verdict)
            write(dest / "judge_usage.json", usage)
        rows.append({"case_id": case.case_id, "judge": verdict})
    write(out / "assessment.json", rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--assess", action="store_true")
    parser.add_argument("--continue-from", type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    fixture = {c["case_id"]: c for c in read(FIXTURE)["cases"] if c["entry"] == "review"}
    case_ids = ([read(args.continue_from / "case.json")["case_id"]] if args.continue_from
                else read(out / "manifest.json")["cases"] if args.assess else CASES)
    cases = [AgentCase.model_validate(fixture[c]["case"]) for c in case_ids]
    assert all(not c.scripted_answers and not c.controlled_web for c in cases)
    if args.assess:
        assess(out, cases)
        return
    cfg = require_agent_llm_config()
    assert cfg.model == "DeepSeek-V4.1-Flash-1017"
    out.mkdir(parents=True, exist_ok=False)
    manifest = {
        "status": "running", "model": cfg.model, "cases": case_ids,
        "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        "prompt_sha256": hashlib.sha256(agent.SYSTEM_PROMPT.encode()).hexdigest(),
        "source_hashes": source_hashes(), "rerank_mode": "off", "ranking_overridden": False,
        "native_fact_ledger_and_verification": True, "additional_review_calls": False,
        "index_writes": 0, "minimum_request_interval_seconds": 40,
        "continuation_from": str(args.continue_from.resolve()) if args.continue_from else None,
    }
    write(out / "manifest.json", manifest)
    bases = {"usage": UsageRequest.__init__, "decide": ToolCallingAgent.decide,
             "search": ComplianceAgentTools.search, "read": ComplianceAgentTools.read_evidence,
             "web": ComplianceAgentTools.search_web}
    last_request = None
    summaries = []
    for case in cases:
        dest = out / case.case_id
        dest.mkdir()
        write(dest / "case.json", case.model_dump(mode="json"))
        calls, research, decisions = [], [], []
        previous_ids = set()
        store = InMemoryEnterpriseStore()
        restored_task = restore_case(store, case, args.continue_from, cfg.model) if args.continue_from else None
        executor = make_production_executor(case, rerank_mode="off", artifacts_dir=dest)
        print("START_REVIEW", case.case_id, flush=True)

        def execute(task, current_store):
            try:
                state = executor(task, current_store)
            except Exception as exc:
                chain = []
                while exc is not None:
                    chain.append({"type": type(exc).__name__, "message": str(exc).replace(cfg.api_key, "[redacted]"),
                                  "http_status": getattr(exc, "code", None)})
                    exc = exc.__cause__
                write(dest / "exception_chain.json", chain)
                raise
            write(dest / "state.json", state.model_dump(mode="json"))
            if state.result:
                write(dest / "delivery.json", state.result)
            return state

        def usage(self, payload):
            nonlocal last_request, previous_ids
            assert cfg.api_key not in json.dumps(payload, ensure_ascii=False)
            if last_request is not None:
                delay = 40 - (time.monotonic() - last_request)
                if delay > 0:
                    time.sleep(delay)
            last_request = time.monotonic()
            bases["usage"](self, payload)
            local, _, web = visible_texts(payload)
            ids = set(local) | set(web)
            if payload.get("tools"):
                assert previous_ids <= ids, "native review lost previously visible original text"
                previous_ids = ids
            number = len(calls) + 1
            record = {"number": number, "phase": self.phase, "visible_ids": sorted(ids),
                      "body_chars": sum(len(h.get("full_article_text") or h.get("text", "")) for h in local.values())}
            calls.append(record)
            write(dest / f"model_{number:02d}.request.json", payload)
            prior = self.sink

            def sink(value):
                record.update(value)
                write(dest / "calls.json", calls)
                if prior:
                    prior(value)

            self.sink = sink
            print("REVIEW_MODEL", case.case_id, number, self.phase, "visible", len(ids), flush=True)

        def decide(self, session, **kwargs):
            decision = bases["decide"](self, session, **kwargs)
            decisions.append({**decision.model_dump(mode="json"), "model_request": len(calls),
                              "previously_visible_ids": sorted(previous_ids)})
            write(dest / "decisions.json", decisions)
            print("REVIEW_ACTION", case.case_id, decision.action, flush=True)
            return decision

        def search(self, *a, **kw):
            result = bases["search"](self, *a, **kw)
            assert len(result) <= kw["max_chunks"] <= 10
            research.append({"kind": "search", "max_chunks": kw["max_chunks"],
                             "queries": [q.model_dump(mode="json") for q in a[0]],
                             "hits": [h.model_dump(mode="json") for h in result]})
            write(dest / "research.json", research)
            return result

        def read_evidence(self, *a, **kw):
            result = bases["read"](self, *a, **kw)
            research.append({"kind": "read", "arguments": list(a[:3]), "selectors": kw,
                             "result": result.model_dump(mode="json")})
            write(dest / "research.json", research)
            return result

        def web_search(self, *a, **kw):
            result = bases["web"](self, *a, **kw)
            research.append({"kind": "web", "queries": [q.model_dump(mode="json") for q in a[0]],
                             "result": result.model_dump(mode="json")})
            write(dest / "research.json", research)
            return result

        open_url = urllib.request.urlopen

        def diagnose_http(*a, **kw):
            try:
                return open_url(*a, **kw)
            except urllib.error.HTTPError as exc:
                write(dest / "http_failure.json", {"status": exc.code, "reason": exc.reason})
                raise

        with ExitStack() as stack:
            for target, name, replacement in [
                (agent_tools, "build_service_adapters", read_only_adapters),
                (UsageRequest, "__init__", usage), (ToolCallingAgent, "decide", decide),
                (ComplianceAgentTools, "search", search), (ComplianceAgentTools, "read_evidence", read_evidence),
                (ComplianceAgentTools, "search_web", web_search),
                (urllib.request, "urlopen", diagnose_http),
            ]:
                stack.enter_context(patch.object(target, name, replacement))
            if restored_task is not None:
                stack.enter_context(patch.object(agent_runner, "build_case_task", return_value=restored_task))
            result = run_agent_case(case, execute=execute, model_id=cfg.model, store=store)
        write(dest / "result.json", result.model_dump(mode="json"))
        # Failed attempts may still have a useful checkpoint and a provisional delivery.
        if not (dest / "state.json").exists():
            for task in store.tasks.values():
                if task.agent_state:
                    write(dest / "state.json", task.agent_state)
        row = {"case_id": case.case_id, "status": result.status,
               "deterministic_pass": result.deterministic_pass,
               "deterministic_fail_reasons": result.deterministic_fail_reasons,
               "failure_message": result.failure_message,
               "actual_model_requests": len(calls), "tool_counts": dict(Counter(r["kind"] for r in research)),
               "prompt_tokens": sum(c.get("prompt_tokens") or 0 for c in calls),
               "completion_tokens": sum(c.get("completion_tokens") or 0 for c in calls),
               "history_retained": True}
        write(dest / "summary.json", row)
        summaries.append(row)
        write(out / "summary.json", summaries)
        print("REVIEW_RESULT", json.dumps(row, ensure_ascii=False), flush=True)
    assert manifest["source_hashes"] == source_hashes()
    manifest.update(status="completed", source_stable=True, completed_runs=len(summaries))
    write(out / "manifest.json", manifest)


if __name__ == "__main__":
    main()
