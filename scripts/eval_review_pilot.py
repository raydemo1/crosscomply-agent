"""Compare fixed retrieval and interactive review with the same production gates."""

from __future__ import annotations

import argparse
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from run_agent_baseline import RecordingStore, write_json

from law_agent.config import require_agent_llm_config, require_semantic_llm_config
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.llm.usage import usage_scope
from law_agent.review.agent import AgentState
from law_agent.review.agent_tools import ComplianceAgentTools
from law_agent.review.evalset.agent_runner import (
    StructuredAgentJudge,
    make_production_executor,
    run_agent_case,
)
from law_agent.review.evalset.delivery_cases import get_review_cases
from law_agent.review.fact_provenance import confirmed_intake_ledger
from law_agent.review.llm import ReviewWorkflowFailed, StructuredLLMNode
from law_agent.review.result_builder import LLMReviewResultDraft, build_result_generation_messages
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH
from law_agent.review.schemas import EvidenceSelfCheck, RetrievalHit, RetrievalQuery, ReviewFacts
from law_agent.review.semantic_grounding import SemanticGroundingRejected
from law_agent.review.workflow import review_needs_source_verification

PILOT_CASES = (
    "agent_confirmed_material_conflict_001", "review_hr_exemption_pending_001",
    "review_customer_transfer_report_001", "review_internal_policy_authority_001",
)


def fixed_executor(case, config, directory):
    """One hybrid retrieval batch and one optional report correction on the same evidence."""
    def execute(task, store):
        intake = store.get_intake_snapshot(task.intake_snapshot_id)
        snapshot = store.get_material_snapshot(task.material_snapshot_id)
        versions = [store.get_material_version(version_id) for version_id in snapshot.version_ids]
        material = "\n\n".join(
            f"【材料 {item.logical_name} v{item.version_number} | {item.id}】\n{item.parsed_text}" for item in versions
        )
        facts = ReviewFacts.model_validate({key: value for key, value in intake.intake.items()
                                            if key in ReviewFacts.model_fields})
        queries = [RetrievalQuery(query_id="fixed-goal", query_type="legal_issue", text=case.question),
                   RetrievalQuery(query_id="fixed-matter", query_type="material_fact", text=case.material_text[:240])]
        tools = ComplianceAgentTools(chunks_path=DEFAULT_CHUNKS_PATH, question=case.question,
                                     material_text=material, material_versions=versions, model_id=config.model)
        try:
            evidence = tools.search(queries, facts, max_chunks=10)
            write_json(directory / "evidence.json", [item.model_dump(mode="json") for item in evidence])
            state = AgentState(goal=case.question, facts=facts, evidence=evidence, queries=queries, searches=1,
                               fact_ledger=confirmed_intake_ledger(intake.intake, intake.id))
            store.checkpoint_agent(task.id, state=state.model_dump(mode="json"), expected_attempt=task.attempt_count)
            node = StructuredLLMNode(node_name="pilot_fixed_report", output_model=LLMReviewResultDraft,
                                     client=OpenAICompatibleClient(config), structured_output_mode="json_object",
                                     stream=True, max_retries=1)
            messages = build_result_generation_messages(
                facts=facts, self_check=EvidenceSelfCheck(status="not_checked"),
                evidence_hits=evidence, question=case.question, material_text=material, retrieval_queries=queries,
            )
            for revision in range(2):
                draft = node.run(messages)
                write_json(directory / f"draft_{revision}.json", draft.model_dump(mode="json"))
                try:
                    payload = tools.finalize(draft, state, case_id=task.case_id,
                                             intake_snapshot={"id": intake.id, "facts": intake.intake})
                except (SemanticGroundingRejected, ValueError) as exc:
                    feedback = exc.verdict.model_dump(mode="json") if isinstance(exc, SemanticGroundingRejected) else str(exc)
                    write_json(directory / f"gate_{revision}.json", feedback)
                    if revision:
                        raise ReviewWorkflowFailed(
                            failed_node="pilot_fixed_report", reason="gate_rejected", message=str(exc), attempts=2,
                        ) from exc
                    messages += [
                        ChatMessage(role="assistant", content=draft.model_dump_json()),
                        ChatMessage(role="user", content="仅用相同证据修正以下核验反馈。\n" + json.dumps(feedback, ensure_ascii=False)),
                    ]
                    continue
                return state.model_copy(update={"status": "completed", "result": payload, "turns": revision + 1})
        finally:
            tools.close()
    return execute


def direct_executor(case, config, evidence, directory):
    """Supply the frozen evidence packet to a single stronger-model report call."""
    def execute(task, _store):
        facts = ReviewFacts.model_validate({key: value for key, value in case.intake.items()
                                            if key in ReviewFacts.model_fields})
        node = StructuredLLMNode(node_name="pilot_direct_report", output_model=LLMReviewResultDraft,
                                 client=OpenAICompatibleClient(config), structured_output_mode="json_object",
                                 stream=True, max_retries=1)
        draft = node.run(build_result_generation_messages(
            facts=facts, self_check=EvidenceSelfCheck(status="not_checked"), evidence_hits=evidence,
            question=case.question, material_text=case.material_text,
        ))
        write_json(directory / "draft.json", draft.model_dump(mode="json"))
        by_id = {hit.chunk_id: hit.model_dump(mode="json") for hit in evidence}
        cited = dict.fromkeys(chunk_id for claim in draft.claims for chunk_id in claim.supporting_chunk_ids)
        invalid_ids = [identifier for identifier in cited if not by_id.get(identifier, {}).get("can_cite_clause")]
        if invalid_ids:
            write_json(directory / "invalid_citations.json", invalid_ids)
        payload = {
            "review_result": {**draft.model_dump(mode="json"), "missing_information": draft.missing_information,
                              "citations": [by_id[identifier] for identifier in cited if identifier in by_id]},
            "evidence_chunks": list(by_id.values()), "web_impact": draft.web_impact,
            "fact_ledger": [
                entry.model_dump(mode="json") for entry in confirmed_intake_ledger(case.intake, task.intake_snapshot_id)
            ],
        }
        payload["freshness_hold"] = review_needs_source_verification(payload)
        write_json(directory / "payload.json", payload)
        return AgentState(goal=case.question, status="completed", result=payload, turns=1)
    return execute


def evaluate(case, arm, config, root, judge_model, evidence=None):
    directory = root / arm / case.case_id
    directory.mkdir(parents=True)
    calls = []
    store = RecordingStore(directory)
    judge = StructuredAgentJudge(model_id=judge_model)

    def judge_with_usage(*args):
        with usage_scope("judge"):
            return judge(*args)

    if arm == "direct":
        execute = direct_executor(case, config, evidence, directory)
    elif arm == "fixed":
        execute = fixed_executor(case, config, directory)
    else:
        execute = make_production_executor(case, artifacts_dir=directory)
    with usage_scope(arm, calls.append):
        result = run_agent_case(case, execute=execute, model_id=config.model, judge=judge_with_usage, store=store)
    record = result.model_dump(mode="json")
    if (directory / "invalid_citations.json").exists():
        record["deterministic_pass"] = record["overall_pass"] = False
        record["deterministic_fail_reasons"].append("invalid_clause_id_in_direct_draft")
    record["requests"] = calls
    write_json(directory / "result.json", record)
    print(json.dumps({"arm": arm, "case": case.case_id, "status": result.status,
                      "deterministic": record["deterministic_pass"], "pass": record["overall_pass"]}), flush=True)
    return {"case": case.case_id, "arm": arm, **record}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--case", action="append")
    parser.add_argument("--arm", choices=("both", "agent", "fixed", "direct"), default="both")
    parser.add_argument("--evidence-from", type=Path, help="Fixed arm results used by the direct model comparison")
    parser.add_argument("--workers", type=int, choices=(1, 2, 4), default=2)
    args = parser.parse_args()
    available = {case.case_id: case for case in get_review_cases() if case.controlled_web is None}
    selected = args.case or PILOT_CASES
    if set(selected) - available.keys() or len(selected) != len(set(selected)):
        parser.error("choose unique formal review cases without a controlled Web fixture")
    cases = [available[case_id] for case_id in selected]
    config = require_agent_llm_config()
    judge_model = config.model
    verifier = require_semantic_llm_config(model_id=config.model)
    evidence_packets = {}
    if args.arm == "direct":
        if args.evidence_from is None:
            parser.error("--arm direct needs --evidence-from")
        evidence_packets = {
            case.case_id: [RetrievalHit.model_validate(item) for item in json.loads(
                (args.evidence_from / "fixed" / case.case_id / "evidence.json").read_text(encoding="utf-8")
            )]
            for case in cases
        }
        config = replace(config, model=verifier.model, reasoning_effort=verifier.reasoning_effort)
    args.output_dir.mkdir(parents=True, exist_ok=False)
    write_json(args.output_dir / "manifest.json", {
        "cases": [case.model_dump(mode="json") for case in cases],
        "model": config.model, "reasoning_effort": config.reasoning_effort,
        "verifier_model": None if args.arm == "direct" else verifier.model,
        "verifier_reasoning_effort": None if args.arm == "direct" else verifier.reasoning_effort,
        "judge_model": judge_model, "trials_per_arm": 1,
        "corpus_sha256": hashlib.sha256(DEFAULT_CHUNKS_PATH.read_bytes()).hexdigest(),
        "fixed_arm": "Two fixed queries in one production hybrid retrieval batch, one permitted revision without more retrieval.",
    })
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        runs = [pool.submit(evaluate, case, arm, config, args.output_dir, judge_model, evidence_packets.get(case.case_id))
                for index, case in enumerate(cases)
                for arm in (("direct",) if args.arm == "direct"
                            else ("fixed", "agent") if index % 2 == 0 else ("agent", "fixed"))
                if args.arm in ("both", arm)]
        write_json(args.output_dir / "summary.json", [run.result() for run in runs])


if __name__ == "__main__":
    main()
