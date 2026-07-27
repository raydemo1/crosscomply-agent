"""Argparse CLI for material-driven review runs."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from law_agent.review.materials import material_from_file, material_from_text
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH
from law_agent.review.service import (
    DEFAULT_REVIEW_RUNS_DIR,
    create_review_case,
    run_service_retrieval,
)


def _cmd_run(args: argparse.Namespace) -> int:
    try:
        material = (
            material_from_text(args.material_text)
            if args.material_text is not None
            else material_from_file(Path(args.material_file), parser=args.parser)
        )
        response = create_review_case(
            question=args.question,
            material=material,
            output_dir=Path(args.output_dir),
            review_mode=args.mode,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Created review case {response.review_case.review_case_id}")
    print(f"Trace {response.trace.trace_id}")
    print(f"Result {response.result.review_result_id}")

    facts = response.review_case.review_facts
    print(f"Facts: cross_border={facts.cross_border_transfer}, data_types={facts.data_types}")
    if facts.industry:
        print(f"  industry={facts.industry}")
    if facts.region:
        print(f"  region={facts.region}")
    if facts.missing_information:
        print(f"  missing={facts.missing_information}")
    print(f"Queries: {len(response.trace.queries)} planned")
    for query in response.trace.queries:
        print(f"  [{query.query_type}] {query.text}")

    print(f"Wrote {response.case_path}")
    print(f"Wrote {response.trace_path}")
    print(f"Wrote {response.result_path}")
    return 0


def _cmd_retrieve(args: argparse.Namespace) -> int:
    try:
        trace = run_service_retrieval(
            case_id=args.case_id,
            chunks_path=Path(args.chunks),
            output_dir=Path(args.output_dir),
            top_k=args.top_k,
            review_mode=args.mode,
            rerank_mode=args.rerank_mode,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(f"Retrieved evidence for case {trace.review_case_id}")
    print(f"Trace {trace.trace_id}")
    print(f"Keyword hits: {len(trace.keyword_results)}")
    print(f"pgvector hits: {len(trace.vector_results)}")
    print(f"Hybrid (RRF) hits: {len(trace.hybrid_results)}")
    print(f"Neighbor chunks: {len(trace.neighbor_chunks)}")
    if trace.metadata_boosts:
        print(f"Metadata boosts: {trace.metadata_boosts}")
    if trace.rerank:
        print(f"Rerank: {trace.rerank}")

    check = trace.evidence_self_check
    print(f"Evidence self-check: {check.status}")
    if check.triggered_reasons:
        print(f"  Triggered reasons: {check.triggered_reasons}")
    for issue in check.issues:
        print(f"  [{issue.issue_type}] {issue.description}")
    if check.second_retrieval_triggered:
        print("  Second retrieval: triggered")
        if trace.second_retrieval:
            print(f"  Expanded queries: {len(trace.second_retrieval.get('expanded_queries', []))}")
    elif check.second_retrieval_plan:
        print("  Second retrieval: planned but not executed")

    if trace.final_evidence:
        print(f"Final evidence: {len(trace.final_evidence)} hits")
    hits = trace.final_evidence or trace.hybrid_results

    for hit in hits:
        print(
            f"  [{hit.rank + 1}] score={hit.score:.4f} role={hit.citation_role} "
            f"can_cite={hit.can_cite_clause} type={hit.matched_query_type}"
        )
        print(f"      {hit.title[:60]}")

    # Issue 8: Display structured review result
    from law_agent.review.io import read_review_results

    results_path = Path(args.output_dir) / "review_results.jsonl"
    if results_path.exists():
        results = read_review_results(results_path)
        result = next(
            (r for r in results if r.review_case_id == args.case_id), None
        )
        if result is not None:
            print(f"\nReview Result: {result.review_result_id}")
            print(f"  Risk level: {result.risk_level}")
            print(f"  Conclusion: {result.conclusion[:120]}")
            if result.trigger_reasons:
                print(f"  Trigger reasons: {result.trigger_reasons}")
            if result.missing_information:
                print(f"  Missing information: {result.missing_information}")
            if result.recommended_actions:
                print("  Recommended actions:")
                for action in result.recommended_actions:
                    print(f"    - {action}")
            if result.risk_boundaries:
                print("  Risk boundaries:")
                for boundary in result.risk_boundaries:
                    print(f"    - {boundary}")
            if result.applicable_evidence:
                print("  Applicable evidence:")
                for group in result.applicable_evidence:
                    print(f"    [{group.usage}] ({len(group.citations)} citations)")
                    if group.scope_note:
                        print(f"      scope: {group.scope_note}")
                    for cite in group.citations[:3]:
                        label = f" - {cite.citation_label}" if cite.citation_label else ""
                        print(f"      {cite.title[:50]}{label}")
    return 0


def _cmd_eval(args: argparse.Namespace) -> int:
    from law_agent.review.evalset.runner import (
        format_summary_markdown,
        format_summary_text,
        run_evaluation,
    )

    try:
        summary = run_evaluation(
            chunks_path=Path(args.chunks),
            suite=args.suite,
            top_k=args.top_k,
            review_mode=args.review_mode,
            rerank_mode=args.rerank_mode,
            max_workers=args.max_workers,
            eval_inputs_path=args.eval_inputs,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(format_summary_text(summary))

    # Save summary JSON if output specified
    if args.output:
        import json

        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            summary.model_dump_json(indent=2), encoding="utf-8"
        )
        print(f"\nSaved JSON summary to {output_path}")

    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            format_summary_markdown(
                summary,
                run_config={
                    "retrieval_mode": "service",
                    "review_mode": args.review_mode,
                    "rerank_mode": args.rerank_mode,
                    "max_workers": args.max_workers,
                    "top_k": args.top_k,
                    "eval_inputs": args.eval_inputs or "default",
                },
            ),
            encoding="utf-8",
        )
        print(f"Saved Markdown report to {report_path}")

    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print(
            "error: uvicorn is not installed. Install with: pip install uvicorn[standard]",
            file=sys.stderr,
        )
        return 2

    from law_agent.review.api import create_app

    app = create_app(
        chunks_path=Path(args.chunks),
        review_mode=args.mode,
    )

    print(f"Starting LawAgent Review API at http://{args.host}:{args.port}")
    print(f"  OpenAPI docs: http://{args.host}:{args.port}/docs")
    print(f"  Corpus: {args.chunks}")
    print(f"  Review mode: {args.mode}")
    print("  Retrieval backend: service")
    print("  Press Ctrl+C to stop")

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


def _cmd_index_service(args: argparse.Namespace) -> int:
    from law_agent.review.retrieval.indexing import (
        write_elasticsearch_bulk_file,
        write_pgvector_rows_file,
    )

    try:
        if args.execute:
            from law_agent.config import require_service_config
            from law_agent.review.retrieval.corpus import load_corpus
            from law_agent.review.retrieval.service_backends import index_corpus_to_services

            config = require_service_config()
            chunks = load_corpus(args.chunks)
            summary = index_corpus_to_services(config, chunks)
            print("Indexed corpus into Elasticsearch + pgvector:")
            for key, value in summary.items():
                print(f"  {key}: {value}")

        if args.elasticsearch_output:
            es_path = write_elasticsearch_bulk_file(
                chunks_path=Path(args.chunks),
                output_path=Path(args.elasticsearch_output),
                index_name=args.elasticsearch_index,
            )
            print(f"Wrote Elasticsearch bulk file: {es_path}")
        if args.pgvector_output:
            pg_path = write_pgvector_rows_file(
                chunks_path=Path(args.chunks),
                output_path=Path(args.pgvector_output),
            )
            print(f"Wrote pgvector rows file: {pg_path}")
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    return 0


def _cmd_service_doctor(args: argparse.Namespace) -> int:
    from law_agent.config import require_service_config
    from law_agent.review.retrieval.service_backends import healthcheck

    try:
        config = require_service_config()
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    result = healthcheck(config)
    print("Service retrieval healthcheck:")
    for key, value in result.items():
        print(f"  {key}: {value}")
    ok = result.get("elasticsearch") and result.get("postgres")
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m law_agent.review")
    subparsers = parser.add_subparsers(dest="command")

    run = subparsers.add_parser("run", help="Create a material review case skeleton")
    run.add_argument("--question", required=True)
    material = run.add_mutually_exclusive_group(required=True)
    material.add_argument("--material-text")
    material.add_argument("--material-file")
    run.add_argument(
        "--parser",
        choices=["auto", "plain", "docx", "docling", "mineru"],
        default="auto",
        help="Parser used for --material-file. Pasted text ignores this option.",
    )
    run.add_argument("--output-dir", default=str(DEFAULT_REVIEW_RUNS_DIR))
    run.add_argument(
        "--mode",
        choices=["llm", "multi_agent"],
        default="llm",
        help="Review owner mode.",
    )
    run.set_defaults(func=_cmd_run)

    retrieve = subparsers.add_parser(
        "retrieve", help="Run retrieval for an existing review case"
    )
    retrieve.add_argument("--case-id", required=True)
    retrieve.add_argument(
        "--chunks",
        default=str(DEFAULT_CHUNKS_PATH),
        help="Path to chunks.jsonl corpus file",
    )
    retrieve.add_argument("--output-dir", default=str(DEFAULT_REVIEW_RUNS_DIR))
    retrieve.add_argument("--top-k", type=int, default=10)
    retrieve.add_argument(
        "--mode",
        choices=["llm", "multi_agent"],
        default="llm",
        help="Review owner mode.",
    )
    retrieve.add_argument(
        "--rerank-mode",
        choices=["off", "embedding"],
        default="off",
        help="Optional post-fusion reranker. Defaults to off.",
    )
    retrieve.set_defaults(func=_cmd_retrieve)

    eval_parser = subparsers.add_parser(
        "eval", help="Run evaluation suite on golden-set scenarios"
    )
    eval_parser.add_argument(
        "--chunks",
        default=str(DEFAULT_CHUNKS_PATH),
        help="Path to chunks.jsonl corpus file",
    )
    eval_parser.add_argument("--top-k", type=int, default=10)
    eval_parser.add_argument(
        "--suite",
        choices=["quick", "base", "full"],
        default="full",
        help="Evaluation suite. quick is a small local smoke set; full is the complete golden set.",
    )
    eval_parser.add_argument(
        "--max-workers",
        type=int,
        default=4,
        help="Number of eval cases to run in parallel. Defaults to 4.",
    )
    eval_parser.add_argument(
        "--review-mode",
        choices=["llm", "multi_agent"],
        default="llm",
        help="Review owner for eval. Defaults to llm.",
    )
    eval_parser.add_argument(
        "--rerank-mode",
        choices=["off", "embedding"],
        default="off",
        help="Optional post-fusion reranker for A/B evaluation. Defaults to off.",
    )
    eval_parser.add_argument(
        "--output",
        default=None,
        help="Save JSON summary to this file path",
    )
    eval_parser.add_argument(
        "--report",
        default=None,
        help="Save a Markdown evaluation report to this file path",
    )
    eval_parser.add_argument(
        "--eval-inputs",
        default=None,
        help="Frozen facts/query JSONL shared by comparable LLM evaluation runs",
    )
    eval_parser.set_defaults(func=_cmd_eval)

    serve = subparsers.add_parser(
        "serve", help="Start the local FastAPI review API server"
    )
    serve.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host to bind (default: 127.0.0.1)",
    )
    serve.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port to bind (default: 8000)",
    )
    serve.add_argument(
        "--chunks",
        default=str(DEFAULT_CHUNKS_PATH),
        help="Path to chunks.jsonl corpus file",
    )
    serve.add_argument(
        "--reload",
        action="store_true",
        help="Enable auto-reload for development",
    )
    serve.add_argument(
        "--mode",
        choices=["llm", "multi_agent"],
        default="llm",
        help="Review owner mode.",
    )
    serve.set_defaults(func=_cmd_serve)

    index_service = subparsers.add_parser(
        "index-service",
        help="Build service retrieval index artifacts for ES and pgvector",
    )
    index_service.add_argument(
        "--chunks",
        default=str(DEFAULT_CHUNKS_PATH),
        help="Path to chunks.jsonl corpus file",
    )
    index_service.add_argument(
        "--elasticsearch-index",
        default="lawagent_chunks",
        help="Target Elasticsearch index name for bulk actions",
    )
    index_service.add_argument(
        "--elasticsearch-output",
        default=None,
        help="Output path for Elasticsearch bulk NDJSON (optional artifact file)",
    )
    index_service.add_argument(
        "--pgvector-output",
        default=None,
        help="Output path for pgvector JSONL rows (optional artifact file)",
    )
    index_service.add_argument(
        "--execute",
        action="store_true",
        help="Execute real import into Elasticsearch + pgvector "
        "(requires ES_URL, PG_DSN, and embedding config in the environment).",
    )
    index_service.set_defaults(func=_cmd_index_service)

    doctor = subparsers.add_parser(
        "service-doctor",
        help="Check Elasticsearch + pgvector reachability for service retrieval",
    )
    doctor.set_defaults(func=_cmd_service_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args)
