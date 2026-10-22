"""Classify candidates for free; run one explicitly selected Q&A case with --real."""

import argparse
import hashlib
import json
from pathlib import Path

from law_agent.review.evalset.delivery_cases import classified_cases, get_qa_cases, get_review_cases
from law_agent.review.evalset.qa_runner import QAAnswerJudge, run_qa_case


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case")
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--judge", action="store_true")
    parser.add_argument("--export-cases", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("输出文件已存在，请使用新的评测路径")
    if args.judge and not args.real:
        parser.error("--judge仅用于显式真实评测")
    report = {"classification": classified_cases(), "evaluation": None}
    if args.export_cases:
        groups = {"qa": get_qa_cases(), "review": get_review_cases()}
        report["cases"] = {name: [case.model_dump(mode="json") for case in cases] for name, cases in groups.items()}
        report["manifest"] = {
            "purpose": "delivery_candidate_review", "approved_cases": 0,
            "evaluation_performed": args.real, "case_counts": {name: len(cases) for name, cases in groups.items()},
            "case_hashes": {case.case_id: hashlib.sha256(case.model_dump_json().encode()).hexdigest()
                            for cases in groups.values() for case in cases},
            "limits": "Candidate definitions only; entry classification is explicit. Existing core and human-review records are unchanged.",
        }
    if args.real:
        case = next((c for c in get_qa_cases() if c.case_id == args.case), None)
        if case is None:
            parser.error("--real必须明确选择一条问答候选案例")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        trace_path = args.output.with_suffix(".checkpoints.jsonl")
        with trace_path.open("x", encoding="utf-8") as stream:
            def checkpoint(changes):
                stream.write(json.dumps(changes, ensure_ascii=False) + "\n")
                stream.flush()
            report["evaluation"] = run_qa_case(case, checkpoint_sink=checkpoint, judge=QAAnswerJudge() if args.judge else None)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    result = report["evaluation"]
    print(json.dumps({"qa_candidates": sum(r["primary_entry"] == "qa" for r in report["classification"]),
                      "review_candidates": sum(r["primary_entry"] == "review" for r in report["classification"]),
                      "evaluation": {k: result[k] for k in ("case_id", "status", "latency_ms", "checks", "judge")} if result else None}, ensure_ascii=False))


if __name__ == "__main__":
    main()
