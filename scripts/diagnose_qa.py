"""Freeze, run and review one-shot QA diagnostics without changing production."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shutil
from dataclasses import asdict
from datetime import date
from decimal import Decimal
from pathlib import Path

from law_agent.config import require_agent_llm_config
from law_agent.llm.openai_compatible import ChatMessage
from law_agent.review.evalset.agent_schemas import AgentCase, AgentRubric
from law_agent.review.evalset.delivery_cases import get_qa_cases
from law_agent.review.evalset.qa_diagnostics import (
    DiagnosticClient,
    Ledger,
    Profile,
    answer_once,
    digest,
    read_json,
    selection_gate,
    write_new,
)
from law_agent.review.evalset.qa_runner import run_qa_case
from law_agent.review.qa_agent import PROMPT, QAState, execute_qa, source_citations
from law_agent.review.qa_context import qa_state_context
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH
from law_agent.review.schemas import RetrievalHit
from law_agent.review.tool_calling import CONTEXT_PROMPT

REPO = Path(__file__).resolve().parents[1]
RUNS = REPO / "data/review_runs"
ORIGINALS = ("eval_cross_border_001", "agent_hr_exception_missing_001",
             "eval_sensitive_002", "eval_standard_contract_003")


def source_hashes():
    paths = [*(REPO / "law_agent").rglob("*.py"), Path(__file__), Path(DEFAULT_CHUNKS_PATH)]
    return {str(p.resolve().relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def original_run(cid):
    if cid in ORIGINALS[:1] or cid == "eval_sensitive_002":
        return read_json(RUNS / f"qa_general_reasoning_20261021/live/{cid}.baseline.json")["run"]
    return read_json(RUNS / f"qa_scope_slices_20261021/{cid}.capture_result.json")["run"]


def history_packet(cid, run):
    if cid in {"eval_cross_border_001", "eval_sensitive_002"}:
        calls = read_json(RUNS / f"qa_general_reasoning_20261021/live/{cid}.baseline.calls.json")
        packet = copy.deepcopy(calls[-1]["request"])
    else:
        packet = read_json(RUNS / f"qa_scope_slices_20261021/{cid}.snapshot.json")
    state = QAState.model_validate(run["state"])
    packet["citations"] = source_citations(state)
    packet["case_id"] = cid
    packet["protocol_matches_current"] = packet["messages"][0]["content"] == PROMPT + CONTEXT_PROMPT
    packet["interpretation"] = "Historical trajectory replay; not a matched current-production experiment"
    return packet


def clean_packet(case, prompt):
    state = QAState(goal=case["question"], current_date=date(2026, 10, 21),
                    evidence=[RetrievalHit.model_validate(h) for h in case["evidence"]])
    citations = source_citations(state)
    return {"case_id": case["case_id"], "citations": citations, "tools": [], "messages": [
        {"role": "system", "content": prompt},
        {"role": "user", "content": json.dumps({"current_date": "2026-10-21", "history": [],
            "material_passages": [{"material_version_id": "diagnostic-input", "offset": 0,
                                   "filename": "用户材料.txt", "text": case["material"]}],
            "state": qa_state_context(state.model_dump(mode="json"))}, ensure_ascii=False)}]}


def prepare(output, *, resume_from=None):
    output.mkdir(parents=True, exist_ok=False)
    cases, packets, histories = [], {}, {}
    original_cases = {c.case_id: c for c in get_qa_cases()}
    changes = {
        "eval_cross_border_001": (
            "补充确认：本企业非CIIO，无重要数据，无特殊免予情形；自2026年1月1日起累计去重实际出境120万人，其中包含1.2万人的敏感个人信息。",
            "依据已明确的实际出境人数判断安全评估机制；不能继续以人数未知回避判断。"),
        "agent_hr_exception_missing_001": (
            "本轮更正：依法签订集体合同已经确认，劳动规章制度与跨境HR管理必要性也已确认；其他事实不变。",
            "不得用一般人数门槛覆盖已满足条件的HR特别免予；不得再次索取已确认的前提。"),
        "eval_sensitive_002": (
            "补充：本项目已生效服务合同明确约定持续向用户提示定位采集状态，采集频率限于实现该目的的最小必要频率。请同时说明这两项约定的约束依据。",
            "区分合同约束、法律义务和标准参考；不能因标准为推荐性而否认给定合同约束，也不能把合同频率说成法律统一要求。"),
        "eval_standard_contract_003": (
            "本轮更正：劳动规章制度和跨境HR管理必要性仍已确认，但是否依法签订集体合同尚未确认；其余事实不变。",
            "集体合同未确认时保留免予成立与不成立分支；不得直接认定无合同或机制已经免予。"),
    }
    categories = dict(zip(ORIGINALS, ("unknown_fact", "special_exception", "reference_authority", "known_premise"), strict=True))
    for cid in ORIGINALS:
        case, saved = original_cases[cid], original_run(cid)
        histories[cid] = history_packet(cid, saved)
        material = case.material_text
        if case.intake:
            material += "\n补充业务信息（用户提供）：\n" + json.dumps(case.intake, ensure_ascii=False)
        row = {"case_id": cid, "pair": categories[cid], "question": case.question,
               "material": material, "evidence": saved["state"]["evidence"],
               "rubric": case.rubric.model_dump(mode="json"), "review_status": "candidate",
               "evidence_origin": "entire saved acquired evidence; no added law excerpts"}
        cases.append(row)
        counterpart = copy.deepcopy(row)
        counterpart.update(case_id=cid + "_counterpart", material=material + "\n" + changes[cid][0],
                           rubric={"target": changes[cid][1]}, synthetic=True)
        cases.append(counterpart)
    for case in cases:
        packets[case["case_id"]] = clean_packet(case, PROMPT)
    heldout_specs = [
        ("旅游代订", "一家非CIIO旅行平台为履行旅客订立的海外酒店预订合同，确需向境外酒店提供旅客普通个人信息，无重要数据，发送范围限于合同履行所必需。", "能否仅因当年累计出境超过100万人，就认定必须安全评估？", "判断合同履行特别免予与一般门槛关系，不把免予解释为所有义务免除。"),
        ("海外售后", "普通非CIIO制造企业拟把国内客户手机号发送至海外售后平台，无重要数据、敏感信息或特殊免予情形；每月处理40万条工单，累计去重出境人数未知。", "单凭这些事实能否确定必须申报安全评估？", "工单条数不等于出境自然人数；不得补造累计人数。"),
        ("资料已确认", "非CIIO集团向境外薪资服务商发送普通员工信息，无重要数据；依法制定劳动规章制度、依法签订集体合同和跨境HR管理必要性均明确确认。本次当年累计出境15万人。", "是否还必须为了人数门槛办理标准合同备案？", "沿用已确认前提，判断特别免予，不附加证明或固定合同记载门槛。"),
        ("内部参考", "企业把一份行业协会的数据管理建议清单作为内部讨论参考；清单建议每季度演练。企业未通过合同或内部制度采纳，也没有其他法源材料。", "能否仅据这份清单认定企业负有每季度演练的法定义务？", "参考清单不能单独形成该频率的法定义务；不编造额外依据。"),
    ]
    heldout = [AgentCase(case_id=f"qa_diagnostic_holdout_{i+1:03}", question=q, material_text=m,
                        rubric=AgentRubric(allowed_judgments=[r]), tags=[tag, "diagnostic_holdout"],
                        selection_reason="New scenario frozen before outputs; not professionally approved")
               .model_dump(mode="json") for i, (tag, m, q, r) in enumerate(heldout_specs)]
    config = require_agent_llm_config()
    profiles = {
        "baseline": asdict(Profile("baseline", config.model, config.base_url,
            "OPENAI_COMPATIBLE_API_KEY", "3", "10", "deepseek", config.beta_base_url)),
        "candidate": asdict(Profile("candidate", "zai-org/GLM-5.3", "https://api.siliconflow.cn/v1",
            "SILICONFLOW_API_KEY", "8", "28", "siliconflow")),
    }
    manifest = {"version": 1, "source_hashes": source_hashes(), "prompt_sha256": digest(PROMPT),
        "cases_sha256": digest(cases), "packets_sha256": digest(packets),
        "histories_sha256": digest(histories), "holdout_sha256": digest(heldout),
        "profiles_sha256": digest(profiles), "budget_cny": "30", "repetitions": 1,
        "max_tokens": 8192, "thinking_budget_candidate": 4096,
        "screen": "At least 2 fewer target-error answers out of 8; no new material errors; complete delivery",
        "transport": "SSE for both providers; connect/read inactivity 30s, total deadline 300s; production unchanged",
        "prices": {"baseline": "Conservative CNY envelope 3/10 per million, above official USD .3/1.2 at 8 CNY/USD; ignore cache/off-peak discounts",
                   "candidate": "Official SiliconFlow GLM-5.3 CNY 8/28 per million; ignore cache discounts",
                   "sources": ["https://api-docs.deepseek.com/quick_start/pricing/", "https://www.siliconflow.cn/pricing"]},
        "limits": "Authored diagnostic candidates; synthetic corrections are latest user statements, not confirmed real-world facts. Historical replay is observational, not causal proof. No judge, resampling, prompt tuning or production switch."}
    if resume_from is not None:
        previous = frozen(resume_from, check_sources=False)
        for key in ("cases_sha256", "packets_sha256", "histories_sha256", "holdout_sha256", "profiles_sha256"):
            if previous[key] != manifest[key]:
                raise RuntimeError("Cannot reuse results with changed input: " + key)
        if not (resume_from / "batch.stopped.json").exists():
            raise RuntimeError("Only an explicitly stopped batch can be resumed")
        reused = []
        for path in sorted(resume_from.glob("fixed.*.result.json")):
            row = read_json(path)
            if row["error"] is not None:
                continue
            if row["input_sha256"] != digest(packets[row["case_id"]]["messages"]):
                raise RuntimeError("Saved answer does not match frozen packet")
            shutil.copyfile(path, output / path.name)
            reused.append(path.name)
            stem = path.name.removesuffix(".result.json")
            for suffix in (".0.request.json", ".0.response.json"):
                shutil.copyfile(resume_from / (stem + suffix), output / (stem + suffix))
        if read_json(resume_from / "protocol.json")["passed"]:
            for name in ("protocol.json", "protocol.0.request.json", "protocol.0.response.json"):
                shutil.copyfile(resume_from / name, output / name)
        old_ledger = Ledger(resume_from / "ledger.jsonl", manifest["budget_cny"])
        carried = {"id": "prior_batch_budget", "state": "reserved", "kind": "carryover",
                   "charged_upper_cny": str(old_ledger.spent)}
        Ledger(output / "ledger.jsonl", manifest["budget_cny"]).append(carried)
        resume = {"previous_directory": str(resume_from.resolve()),
                  "previous_manifest_sha256": digest(previous), "reused_results": reused,
                  "carried_budget_upper_cny": str(old_ledger.spent),
                  "previous_requests": sum(r["state"] == "reserved" for r in old_ledger.entries),
                  "reason": "User explicitly resumed diagnostics; reuse completed answers and attempt only unfinished items"}
        write_new(output / "resume.json", resume)
        manifest["resume_sha256"] = digest(resume)
    for name, value in (("cases", cases), ("packets", packets), ("histories", histories),
                        ("heldout", heldout), ("profiles", profiles), ("manifest", manifest)):
        write_new(output / f"{name}.json", value)
    print(json.dumps({"prepared": str(output), "fixed_requests": 16, "history_requests": 4}, ensure_ascii=False))


def frozen(output, *, check_sources=True):
    manifest = read_json(output / "manifest.json")
    for name in ("cases", "packets", "histories", "heldout", "profiles"):
        if digest(read_json(output / f"{name}.json")) != manifest[f"{name if name != 'heldout' else 'holdout'}_sha256"]:
            raise RuntimeError("Frozen input changed: " + name)
    if check_sources and source_hashes() != manifest["source_hashes"]:
        raise RuntimeError("Source or corpus changed since freeze")
    if "resume_sha256" in manifest and digest(read_json(output / "resume.json")) != manifest["resume_sha256"]:
        raise RuntimeError("Resume provenance changed")
    return manifest


def profiles_for(output):
    config = require_agent_llm_config()
    # Existing production config is read, never overwritten or copied to disk.
    if config.api_key:
        os.environ.setdefault("OPENAI_COMPATIBLE_API_KEY", config.api_key)
    profiles = {arm: Profile(**p) for arm, p in read_json(output / "profiles.json").items()}
    baseline = profiles["baseline"]
    if (config.model != baseline.model or config.base_url != baseline.base_url
            or config.beta_base_url != baseline.beta_base_url or config.reasoning_effort != "low"):
        raise RuntimeError("Active baseline configuration differs from frozen profile")
    return profiles


def run_protocol(output):
    manifest = frozen(output)
    profile = profiles_for(output)["candidate"]
    ledger = Ledger(output / "ledger.jsonl", manifest["budget_cny"])
    client = DiagnosticClient(profile, ledger, output, "protocol", max_tokens=512)
    tools = [{"type": "function", "function": {"name": "echo", "strict": True,
        "description": "Return the supplied literal", "parameters": {"type": "object",
            "properties": {"value": {"type": "string", "enum": ["ok"]}},
            "required": ["value"], "additionalProperties": False}}}]
    result = {"passed": False}
    try:
        message = client.chat_tools([ChatMessage(role="user", content="请调用echo工具，value为ok，不要直接回答。")], tools=tools)
        calls = message.get("tool_calls") or []
        result["passed"] = (len(calls) == 1 and calls[0]["function"]["name"] == "echo"
                             and json.loads(calls[0]["function"]["arguments"]) == {"value": "ok"})
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        result["error"] = type(exc).__name__
    write_new(output / "protocol.json", result)
    print(json.dumps(result))


def run_answers(output, phase):
    manifest = frozen(output)
    if (output / "batch.stopped.json").exists():
        raise RuntimeError("Batch is stopped; existing failed attempts must not be retried")
    if not read_json(output / "protocol.json")["passed"]:
        raise RuntimeError("Candidate protocol failed; no diagnostic fallback")
    profiles = profiles_for(output)
    ledger = Ledger(output / "ledger.jsonl", manifest["budget_cny"])
    packets = read_json(output / ("packets.json" if phase == "fixed" else "histories.json"))
    order = [(cid, arm) for i, cid in enumerate(packets)
             for arm in (("baseline", "candidate") if i % 2 == 0 else ("candidate", "baseline"))]
    if phase == "history":
        order = [(cid, "baseline") for cid in packets]
    for cid, arm in order:
        path = output / f"{phase}.{cid}.{arm}.result.json"
        if path.exists():
            continue
        client = DiagnosticClient(profiles[arm], ledger, output, f"{phase}.{cid}.{arm}", max_tokens=manifest["max_tokens"])
        result = answer_once(client, packets[cid])
        result.update(case_id=cid, arm=arm, phase=phase)
        write_new(path, result)
        print(json.dumps({k: result[k] for k in ("case_id", "arm", "error", "latency_ms")}), flush=True)
        if result["error"] and result["error"].startswith("HTTP "):
            write_new(output / "batch.stopped.json", {"phase": phase, "case_id": cid,
                      "arm": arm, "error": result["error"], "reason": "systemic_http_failure"})
            raise RuntimeError("HTTP failure stops the batch; no fallback or further calls")


def gate(output, *, check_sources=True):
    frozen(output, check_sources=check_sources)
    rows = [read_json(p) for p in sorted(output.glob("fixed.*.result.json"))]
    reviews_path = output / "reviews.json"
    if not reviews_path.exists():
        write_new(reviews_path, [{"case_id": r["case_id"], "arm": r["arm"], "result_sha256": digest(r),
            "target_error": None, "material_error": None, "new_material_error": None,
            "quote": "", "evidence": "", "reason": ""} for r in rows])
    return selection_gate(rows, read_json(reviews_path), list(read_json(output / "packets.json")))


def run_live(output):
    manifest = frozen(output)
    decision = gate(output)
    if not decision["eligible"]:
        raise RuntimeError("Held-out runs blocked by screening gate: " + json.dumps(decision))
    write_new(output / "live.selection.json", {"gate": decision, "reviews_sha256": digest(read_json(output / "reviews.json"))})
    profiles = profiles_for(output)
    ledger = Ledger(output / "ledger.jsonl", manifest["budget_cny"])
    for i, value in enumerate(read_json(output / "heldout.json")):
        case = AgentCase.model_validate(value)
        for arm in (("baseline", "candidate") if i % 2 == 0 else ("candidate", "baseline")):
            stem = f"live.{case.case_id}.{arm}"
            client = DiagnosticClient(profiles[arm], ledger, output, stem)

            def execute(conversation, run, *, checkpoint, chunks_path, client=client):
                return execute_qa(conversation, run, checkpoint=checkpoint, chunks_path=chunks_path, client=client)

            result = run_qa_case(case, execute=execute)
            write_new(output / f"{stem}.result.json", result)
            # QAWorker catches failures; a budget/protocol failure must stop the batch.
            if result["status"] != "completed":
                raise RuntimeError("Held-out run failed; stop without resampling")
            print(json.dumps({"case_id": case.case_id, "arm": arm, "status": result["status"]}), flush=True)


def report(output):
    manifest = frozen(output, check_sources=False)
    rows = [read_json(p) for p in sorted(output.glob("*.result.json"))]
    ledger = Ledger(output / "ledger.jsonl", manifest["budget_cny"])
    latencies = [r["latency_ms"] for r in rows if "latency_ms" in r]
    accounting = {}
    for path in output.glob("*.response.json"):
        record = read_json(path)
        response = record.get("response") or {}
        if not response.get("usage"):
            continue
        model = response.get("model", "unknown")
        totals = accounting.setdefault(model, {"requests": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "cache_hit_tokens": 0, "latency_ms": []})
        totals["requests"] += 1
        for key in ("prompt_tokens", "completion_tokens"):
            totals[key] += response["usage"].get(key, 0)
        totals["cache_hit_tokens"] += response["usage"].get("prompt_cache_hit_tokens",
            (response["usage"].get("prompt_tokens_details") or {}).get("cached_tokens", 0))
        totals["latency_ms"].append(record["latency_ms"])
    for totals in accounting.values():
        values = totals.pop("latency_ms")
        totals["latency_ms_range"] = [min(values), max(values)]
    latest = {row["id"]: row for row in ledger.entries}
    current_hashes = source_hashes()
    changed = [path for path, hashed in manifest["source_hashes"].items()
               if current_hashes.get(path) != hashed]
    summary = {"results": len(rows), "conservative_cost_cny": str(ledger.spent),
        "requests_in_this_batch": sum(r["state"] == "reserved" and r.get("kind") != "carryover"
                                      for r in ledger.entries),
        "settled_cost_upper_cny": str(sum((Decimal(r["charged_upper_cny"])
            for r in latest.values() if r["state"] == "settled"), start=Decimal(0))),
        "unsettled_requests": [key for key, r in latest.items() if r["state"] == "reserved"],
        "usage_by_response_model": accounting,
        "latency_ms_range": [min(latencies), max(latencies)] if latencies else [],
        "screen": gate(output, check_sources=False),
        "production_changed": any(path.replace("\\", "/") not in {
            "law_agent/review/evalset/qa_diagnostics.py", "scripts/diagnose_qa.py"} for path in changed),
        "source_matches_frozen": not changed, "changed_since_freeze": changed,
        "limits": "One attempt per problem/arm; authored candidates, no legal accuracy or stability claim"}
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=["prepare", "protocol", "fixed", "history", "gate", "live", "report"])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume-from", type=Path)
    args = parser.parse_args()
    if args.phase == "prepare":
        prepare(args.output, resume_from=args.resume_from)
    elif args.phase == "protocol":
        run_protocol(args.output)
    elif args.phase in {"fixed", "history"}:
        run_answers(args.output, args.phase)
    elif args.phase == "gate":
        print(json.dumps(gate(args.output), ensure_ascii=False))
    elif args.phase == "live":
        run_live(args.output)
    else:
        report(args.output)


if __name__ == "__main__":
    main()
