"""Run paired, author-labeled verifications through the production verifier."""

from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from time import monotonic
from unittest.mock import patch

from run_agent_baseline import write_json

from law_agent.config import require_semantic_llm_config
from law_agent.llm.usage import usage_scope
from law_agent.review import semantic_grounding
from law_agent.review.result_builder import LLMReviewResultDraft
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH, load_corpus
from law_agent.review.retrieval.neighbors import hit_from_chunk
from law_agent.review.schemas import FactLedgerEntry, RetrievalHit, ReviewFacts

RULES = "cac_cross_border_data_flow_rules_2024"
PIPL = "flk_npc_ff8081817b6472a3017b656cc2040044"
FAQ = "cac_data_export_policy_qna_2026_07:2961b0508c679061a2f863ab"
REVIEW_DATE = date(2026, 10, 8)


def fixtures(chunks):
    rules = {c.article_no: c.chunk_id for c in chunks.values() if c.source_id == RULES}
    pipl = {c.article_no: c.chunk_id for c in chunks.values() if c.source_id == PIPL}
    current = "北京非自贸区的非关键信息基础设施运营者，已明确无重要数据、无敏感个人信息、不适用第四至第六条免予情形。自当年1月1日起累计向境外提供12万名自然人的普通个人信息。"
    path = "订立个人信息出境标准合同或通过个人信息保护认证"
    summary = "现有确认事实支持订立个人信息出境标准合同或通过个人信息保护认证。材料未提供签署、认证或备案完成证明。"
    draft = {
        "risk_level": "medium",
        "legal_path": path,
        "decision_summary": summary,
        "conclusion": summary,
        "claims": [{"text": "本案进入标准合同或认证机制，缺少手续凭据不证明手续已经完成。",
                    "supporting_chunk_ids": [rules["第八条"]]}],
    }
    evidence = [rules["第五条"], rules["第七条"], rules["第八条"]]
    hr = "跨境人力资源管理确需向境外提供内部员工个人信息，劳动规章制度已依法制定。未确认是否依法订立集体合同。"
    hr_summary = "跨境人力资源管理的必要性和劳动规章制度已确认。集体合同尚未核实，应补证，当前无法确定能否适用相关免予情形。"
    hr_draft = {
        "risk_level": "insufficient_evidence",
        "decision_summary": hr_summary,
        "conclusion": hr_summary,
        "questions": [{"text": "是否已依法订立集体合同？"}],
        "claims": [{"text": "满足第五条第二项要件时可适用该项免予程序，现有事实仍有必要前提未确认。",
                    "supporting_chunk_ids": [rules["第五条"]]}],
    }
    hr_confirmed = "本次仅核对告知及同意义务。已确认确有跨境人力资源管理必要性，依法制定劳动规章制度且依法订立集体合同。"
    hr_consent = "在已确认适用个人信息保护法第十三条第一款第二项的情况下，无需取得个人同意，仍应按第三十九条履行告知义务。"
    hr_consent_draft = {
        "risk_level": "low",
        "decision_summary": hr_consent,
        "conclusion": hr_consent,
        "claims": [{"text": hr_consent, "supporting_chunk_ids": [pipl["第十三条"], pipl["第三十九条"]]}],
        "reference_chunk_ids": [FAQ],
        "recommended_actions": ["按第三十九条履行向个人的告知义务。"],
    }
    return [
        ("sensitive_threshold", "核对本案出境机制和数据规模变化的风险边界", current,
         evidence, {**draft,
                    "conclusion": summary + " 若以后当年累计敏感个人信息达到1万人，则应申报安全评估。",
                    "risk_boundaries": ["若以后当年累计敏感个人信息达到1万人，则应申报安全评估。"]},
         "risk_boundaries", ["一旦出现敏感个人信息，就应申报安全评估。"], "risk_boundaries:0"),
        ("unknown_collective_agreement", "核对人力资源管理免予情形是否成立", hr,
         evidence, hr_draft, "decision_summary",
         "跨境人力资源管理的必要性和劳动规章制度已确认。由于本案未依法订立集体合同，该项免予情形已确定不能适用。",
         "decision_summary"),
        ("consent_exception", "核对现有事实下适用的告知及同意义务", hr_confirmed,
         [pipl["第十三条"], pipl["第三十九条"], FAQ], hr_consent_draft, "recommended_actions",
         ["无论是否符合个人信息保护法第十三条第一款第二项，均应当取得个人的单独同意。"],
         "recommended_actions:0"),
        ("premature_clearance", "判断适用机制，区分已完成的手续与拟采取的措施", current + "尚未提供手续完成证明。",
         evidence, draft, "decision_summary",
         "现有确认事实支持采用标准合同或认证机制。本案标准合同手续及备案均已完成，业务已经获批，可以立即出境执行。",
         "decision_summary"),
    ]


def run_one(job, chunks, root, model, repeat):
    case_id, goal, material, sources, clean, field, mutation, target = job
    records = []
    variants = ("clean", "mutated") if repeat % 2 else ("mutated", "clean")
    for variant in variants:
        draft = LLMReviewResultDraft.model_validate(clean if variant == "clean" else {**clean, field: mutation})
        evidence = [hit_from_chunk(chunks[chunk_id], rank) for rank, chunk_id in enumerate(sources, 1)]
        usage = []
        record = {"case": case_id, "variant": variant, "repeat": repeat, "expected_target": target, "requests": usage}
        start = monotonic()
        try:
            with usage_scope("verifier_pilot", usage.append):
                verdict = semantic_grounding.SemanticGroundingVerifier(model_id=model)(
                    review_goal=goal, draft=draft, confirmed_intake={}, extracted_facts=ReviewFacts(),
                    material=material, evidence=evidence, chunks_by_id=chunks, as_of_date=REVIEW_DATE,
                )
            record.update(status=verdict.status, verdict=verdict.model_dump(mode="json"),
                          located=target in {check.target for check in [*verdict.core_checks, *verdict.report_checks]})
        except (ValueError, RuntimeError) as exc:
            record.update(status="error", error_type=type(exc).__name__, error=str(exc))
        record["seconds"] = round(monotonic() - start, 2)
        write_json(root / f"{case_id}.{variant}.{repeat}.json", record)
        print(json.dumps({k: record[k] for k in ("case", "variant", "repeat", "status", "seconds")}), flush=True)
        records.append(record)
    return records


def observed_jobs(root):
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    cases = {item["case_id"]: item for item in manifest["cases"]}
    targets = {
        "agent_confirmed_material_conflict_001": "risk_boundaries:2",
        "review_hr_exemption_pending_001": "risk_boundaries:0",
        "review_customer_transfer_report_001": None,
        "review_internal_policy_authority_001": None,
    }
    jobs = []
    for case_id, target in targets.items():
        directory = root / "agent" / case_id
        state = json.loads((directory / "state.json").read_text(encoding="utf-8"))
        drafts = [
            json.loads(call["function"]["arguments"])["draft"]
            for message in state["tool_session"]["messages"]
            for call in message.get("tool_calls") or []
            if call["function"]["name"] == "finish"
        ]
        if not state.get("result") or not drafts:
            raise ValueError(f"Missing final draft or review result for {case_id}")
        jobs.append({
            "case": case_id, "expected_target": target, "draft": drafts[-1],
            "review_goal": cases[case_id]["question"], "material": cases[case_id]["material_text"],
            "confirmed_intake": cases[case_id]["intake"], "facts": state["facts"],
            "evidence": state["evidence"], "fact_ledger": state["fact_ledger"],
        })
    return jobs


def replay_observed(job, chunks, root, model):
    usage = []
    started = monotonic()
    target = job["expected_target"]
    record = {"case": job["case"], "expected_target": target, "requests": usage}
    try:
        with usage_scope("verifier_observed", usage.append):
            verdict = semantic_grounding.SemanticGroundingVerifier(model_id=model)(
                review_goal=job["review_goal"], draft=LLMReviewResultDraft.model_validate(job["draft"]),
                confirmed_intake=job["confirmed_intake"], extracted_facts=ReviewFacts.model_validate(job["facts"]),
                material=job["material"], evidence=[RetrievalHit.model_validate(hit) for hit in job["evidence"]],
                chunks_by_id=chunks, fact_ledger=[FactLedgerEntry.model_validate(item) for item in job["fact_ledger"]],
                as_of_date=REVIEW_DATE,
            )
        record.update(status=verdict.status, verdict=verdict.model_dump(mode="json"),
                      located=target in {check.target for check in [*verdict.core_checks, *verdict.report_checks]})
    except (ValueError, RuntimeError) as exc:
        record.update(status="error", error=str(exc))
    record["seconds"] = round(monotonic() - started, 2)
    write_json(root / f"{job['case']}.json", record)
    print(json.dumps({k: record[k] for k in ("case", "status", "seconds")}), flush=True)
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prompt-addon", type=Path)
    parser.add_argument("--observed-run", type=Path, help="Replay four final drafts from the production comparison")
    parser.add_argument("--workers", type=int, choices=(1, 2, 4), default=2)
    parser.add_argument("--repeats", type=int, choices=(1, 2, 3), default=1)
    args = parser.parse_args()
    chunks = {c.chunk_id: c for c in load_corpus()}
    jobs = observed_jobs(args.observed_run) if args.observed_run else fixtures(chunks)
    config = require_semantic_llm_config()
    prompt = semantic_grounding.SYSTEM_PROMPT
    if args.prompt_addon:
        prompt += "\n" + args.prompt_addon.read_text(encoding="utf-8").strip()
    args.output_dir.mkdir(parents=True, exist_ok=False)
    write_json(args.output_dir / "manifest.json", {
        "model": config.model, "reasoning_effort": config.reasoning_effort,
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
        "corpus_sha256": hashlib.sha256(DEFAULT_CHUNKS_PATH.read_bytes()).hexdigest(),
        "review_date": REVIEW_DATE.isoformat(), "fixtures": jobs,
        "fixture_scope": ("Final runtime drafts with two targeted historical failure patterns and two controls."
                          if args.observed_run else "Four author-labeled one-field mutations with frozen corpus evidence."),
        "repeats": args.repeats,
    })
    with patch.object(semantic_grounding, "SYSTEM_PROMPT", prompt), ThreadPoolExecutor(max_workers=args.workers) as pool:
        if args.observed_run:
            records = list(pool.map(lambda job: replay_observed(job, chunks, args.output_dir, config.model), jobs))
            write_json(args.output_dir / "summary.json", records)
            return
        futures = [pool.submit(run_one, job, chunks, args.output_dir, config.model, repeat)
                   for repeat in range(1, args.repeats + 1) for job in jobs]
        records = [record for future in futures for record in future.result()]
    summary = {
        "total": len(records),
        "correct_clean": sum(r["status"] == "supported" for r in records if r["variant"] == "clean"),
        "false_rejections": sum(r["status"] in {"unsupported", "uncertain"} for r in records if r["variant"] == "clean"),
        "mutations_caught": sum(r["status"] in {"unsupported", "uncertain"} for r in records if r["variant"] == "mutated"),
        "mutations_located": sum(r.get("located", False) for r in records if r["variant"] == "mutated"),
        "errors": sum(r["status"] == "error" for r in records),
        "runs": records,
    }
    write_json(args.output_dir / "summary.json", summary)
    print(json.dumps({k: v for k, v in summary.items() if k != "runs"}), flush=True)


if __name__ == "__main__":
    main()
