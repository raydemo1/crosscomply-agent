"""Run candidate cases with durable evidence, without changing production code."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from law_agent.config import load_service_config, load_web_search_api_key, require_llm_config
from law_agent.review.enterprise_store import InMemoryEnterpriseStore
from law_agent.review.evalset.agent_cases import get_agent_cases
from law_agent.review.evalset.agent_review import cases_from_selection
from law_agent.review.evalset.agent_runner import (
    StructuredAgentJudge,
    infrastructure_http_status,
    make_production_executor,
    run_agent_case,
)
from law_agent.review.evalset.agent_schemas import AgentCase, AgentCaseResult
from law_agent.review.ids import utc_now_iso
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH


def write_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class RecordingStore(InMemoryEnterpriseStore):
    def __init__(self, directory: Path):
        super().__init__()
        self.directory = directory
        self.seen_steps = 0

    def event(self, kind: str, value: object) -> None:
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": utc_now_iso(), "kind": kind, "value": value}, ensure_ascii=False, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def save_state(self, state: dict) -> None:
        write_json(self.directory / "state.json", state)
        steps = state.get("steps", [])
        for step in steps[self.seen_steps:]:
            self.event("step", step)
        self.seen_steps = len(steps)
        self.event("checkpoint", {"turns": state.get("turns"), "steps": self.seen_steps, "status": state.get("status")})

    def checkpoint_agent(self, task_id, *, state, expected_attempt=None):
        task = super().checkpoint_agent(task_id, state=state, expected_attempt=expected_attempt)
        self.save_state(state)
        return task


def failure_categories(result: AgentCaseResult) -> list[str]:
    categories: set[str] = set()
    if result.status == "failed":
        categories.add("runtime_or_infrastructure")
    if result.status == "blocked":
        categories.add("provider_infrastructure")
    if result.status == "unanswered_gate":
        categories.add("clarification_unanswered")
    if result.abstain_correct is False:
        categories.add("abstention")
    if result.missing_required_sources:
        categories.add("source_coverage")
    if result.illegal_clause_citations:
        categories.add("citation")
    if result.freshness_hold_correct is False:
        categories.add("freshness")
    if result.outcome_acceptable is False:
        categories.add("legal_outcome")
    if result.budget_exhausted:
        categories.add("budget_exhaustion")
    if result.judge:
        if result.judge.error:
            categories.add("judge_infrastructure")
        for field, category in [("legal_correctness", "legal_reasoning"), ("exception_coverage", "missed_exception"), ("fact_grounding", "fact_grounding"), ("clarification_quality", "clarification")]:
            if getattr(result.judge, field) in ("minor_issue", "fail"):
                categories.add(category)
    return sorted(categories)


def write_report(root: Path, cases: list[AgentCase], records: dict[str, dict], *, filename: str = "report.md") -> None:
    lines = [
        "# Production Agent 候选基线",
        "",
        "本批为未人工审定的合成候选案件。PASS仅表示符合当前候选rubric，不代表已确认法律可靠性。生产版本见manifest，运行期间是否变更见summary；不同生产版本的结果不得合并计算。",
        "",
        "| 案例 | 审定状态 | 总评 | 运行状态 | 风险等级 | 轮数 | 分类 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for case in cases:
        r = records.get(case.case_id)
        if r is None:
            continue
        verdict = "PASS" if r["overall_pass"] is True else "FAIL" if r["overall_pass"] is False else "UNEVALUATED"
        lines.append(f"| {case.case_id} | {case.review_status} | {verdict} | {r.get('status', '-')} | {r.get('risk_level', '-')} | {r.get('turns', '-')} | {', '.join(r['failure_categories']) or '-'} |")
    for case in cases:
        r = records.get(case.case_id)
        if r is None:
            continue
        directory = root / case.case_id
        payload_path = directory / "payload.json"
        state_path = directory / "state.json"
        payload = json.loads(payload_path.read_text(encoding="utf-8")) if payload_path.exists() else {}
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        rr = payload.get("review_result", {})
        lines.extend(["", f"## {case.case_id}", "", f"选题：{case.selection_reason}", "", f"问题：{case.question}", "", "允许判断：", ""])
        lines.extend(f"- {x}" for x in case.rubric.allowed_judgments)
        lines.extend(["", "禁止判断：", ""])
        lines.extend(f"- {x}" for x in case.rubric.forbidden_judgments)
        lines.extend(["", f"实际法律路径：{rr.get('legal_path', '未交付')}", "", f"实际结论：{rr.get('conclusion', '未交付')}", "", f"确定性问题：{r.get('deterministic_fail_reasons', [])}", ""])
        if r.get("error") or r.get("failure_message"):
            lines.extend([f"运行问题：{r.get('error') or r['failure_message']}", ""])
        judge = r.get("judge") or {}
        lines.extend(f"- Judge：{reason}" for reason in judge.get("reasons", []))
        if judge.get("error"):
            lines.extend([f"- Judge故障：{judge['error']}"])
        retrieved = {e["source_id"] for e in state.get("evidence", [])}
        missing = r.get("missing_required_sources", [])
        if missing:
            lines.extend(["", f"法源诊断：缺失引用中已找到的来源={sorted(set(missing) & retrieved)}；未进入最终证据的来源={sorted(set(missing) - retrieved)}。后者仍需结合工具记录判断是检索失败还是选择遗漏。"])
        rejections = [s for s in state.get("steps", []) if s.get("observation", {}).get("semantic_grounding")]
        if rejections:
            lines.extend(["", "生产语义门禁拒绝记录：", ""])
            for step in rejections:
                gate = step["observation"]["semantic_grounding"]
                lines.append(f"- 第{step['number']}步 `{gate['status']}`：{gate.get('conclusion_reason', '')}")
        validation_errors = Counter(s["observation"]["error"] for s in state.get("steps", []) if s.get("action") == "finish" and s.get("observation", {}).get("error"))
        if validation_errors:
            lines.extend(["", "报告提交失败记录（最终评分通过也保留）：", ""])
            lines.extend(f"- {count}次：{error}" for error, count in validation_errors.items())
        lines.extend(["", f"证据：[输入与rubric]({case.case_id}/case.json) · [逐步记录]({case.case_id}/events.jsonl) · [checkpoint]({case.case_id}/state.json) · [评分]({case.case_id}/result.json)", ""])
    (root / filename).write_text("\n".join(lines), encoding="utf-8")


def run_one(case: AgentCase, directory: Path, *, model: str, judge_model: str, blocked: threading.Event) -> dict:
    directory.mkdir()
    write_json(directory / "case.json", case.model_dump(mode="json"))
    if blocked.is_set():
        record = {"case_id": case.case_id, "review_status": case.review_status, "status": "blocked", "overall_pass": None, "failure_categories": ["provider_infrastructure"], "error": "服务已出现确定性基础设施阻断；本案未启动模型调用。"}
        write_json(directory / "result.json", record)
        return record
    store = RecordingStore(directory)
    store.event("started", {"case_id": case.case_id, "review_status": case.review_status})
    result = run_agent_case(
        case,
        execute=make_production_executor(case, artifacts_dir=directory),
        model_id=model,
        judge=StructuredAgentJudge(model_id=judge_model, on_input=lambda messages: write_json(directory / "judge_input.json", [{"role": m.role, "content": m.content} for m in messages])),
        store=store,
    )
    for task in store.tasks.values():
        if task.agent_state:
            store.save_state(task.agent_state)
        if task.result:
            write_json(directory / "payload.json", task.result)
    record = result.model_dump(mode="json")
    record["failure_categories"] = failure_categories(result)
    provider_message = result.failure_message or (result.judge.error if result.judge else "") or ""
    if infrastructure_http_status(provider_message) in (401, 402, 403, 429):
        blocked.set()
    write_json(directory / "result.json", record)
    store.event("completed", record)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--round", type=int, choices=[1, 2])
    selection.add_argument("--selection-plan", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true", help="validate selection without model calls")
    parser.add_argument("--workers", type=int, default=2, choices=[1, 2])
    parser.add_argument("--judge-model", default=None)
    parser.add_argument("--case", action="append", dest="case_ids", default=None)
    args = parser.parse_args()
    plan = None
    if args.selection_plan:
        try:
            plan = json.loads(args.selection_plan.read_text(encoding="utf-8"))
            cases = cases_from_selection(plan)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            parser.error(str(exc))
    else:
        cases = [c for c in get_agent_cases("core") if c.construction_round == args.round]
    if args.case_ids:
        unknown = set(args.case_ids) - {c.case_id for c in cases}
        if unknown:
            parser.error(f"unknown case IDs in selected set: {sorted(unknown)}")
        cases = [c for c in cases if c.case_id in args.case_ids]
    if not cases:
        parser.error("this selection has no cases")
    if args.workers != 1 and any(c.controlled_web for c in cases):
        parser.error("controlled Web fixtures require --workers 1")
    if args.dry_run:
        print(json.dumps({"mode": "dry_run", "case_count": len(cases),
                          "case_ids": [c.case_id for c in cases], "model_calls": 0,
                          "evaluation_performed": False}, ensure_ascii=False))
        return 0
    if args.output_dir is None:
        parser.error("--output-dir is required for a model run")
    config = require_llm_config()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    if plan is not None:
        write_json(args.output_dir / "selection.json", plan)
    production_files = sorted(p for p in Path("law_agent").rglob("*.py") if "evalset" not in p.parts)
    production_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in production_files}
    service_config = load_service_config()
    write_json(args.output_dir / "manifest.json", {
        "started_at": utc_now_iso(), "round": args.round,
        "agent_model": config.model, "judge_model": args.judge_model or config.model,
        "reasoning_effort": config.reasoning_effort, "structured_output_mode": config.structured_output_mode,
        "rerank_mode": "off", "workers": args.workers,
        "judge_protocol": "final_delivery_cited_evidence_fact_ledger_v1",
        "working_tree_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()),
        "embedding_provider": service_config.embedding.provider,
        "embedding_model": service_config.embedding.model,
        "es_index": service_config.elasticsearch.index_name,
        "pg_table": service_config.postgres.table_name,
        "web_search_configured": bool(load_web_search_api_key()),
        "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "corpus_sha256": hashlib.sha256(DEFAULT_CHUNKS_PATH.read_bytes()).hexdigest(),
        "source_manifest_sha256": hashlib.sha256(DEFAULT_CHUNKS_PATH.with_name("source_manifest.csv").read_bytes()).hexdigest(),
        "production_sha256": production_hashes,
        "evaluation_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__), *Path("law_agent/review/evalset").glob("agent_*.py")]},
        "cases": [c.model_dump(mode="json") for c in cases],
        "boundary": "未人工审定的合成候选案件；真实生产runtime/检索/模型；独立评测judge默认同模型，不能替代人工审定。",
    })
    records: dict[str, dict] = {}
    lock = threading.Lock()
    blocked = threading.Event()

    def save_status() -> None:
        write_json(args.output_dir / "status.json", {"updated_at": utc_now_iso(), "total": len(cases), "completed": len(records), "results": list(records.values())})

    save_status()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {pool.submit(run_one, case, args.output_dir / case.case_id, model=config.model, judge_model=args.judge_model or config.model, blocked=blocked): case for case in cases}
        for future in as_completed(jobs):
            case = jobs[future]
            try:
                record = future.result()
            except Exception as exc:  # noqa: BLE001 - preserve other completed cases
                record = {"case_id": case.case_id, "review_status": case.review_status, "overall_pass": None, "failure_categories": ["evaluation_infrastructure"], "error": f"{type(exc).__name__}: {exc}"}
                write_json(args.output_dir / case.case_id / "result.json", record)
            with lock:
                records[case.case_id] = record
                save_status()
                write_report(args.output_dir, cases, records)
            print(json.dumps({"case_id": case.case_id, "overall_pass": record["overall_pass"], "categories": record["failure_categories"]}, ensure_ascii=False), flush=True)
    changed_files = [str(p) for p in production_files if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != production_hashes[str(p)]]
    write_json(args.output_dir / "summary.json", {"finished_at": utc_now_iso(), "round": args.round, "candidate_count": sum(c.review_status == "candidate" for c in cases), "production_changed_during_run": changed_files, "results": [records[c.case_id] for c in cases]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
