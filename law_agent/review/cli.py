"""Argparse CLI for material-driven review runs."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH


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
    )

    print(f"Starting LawAgent Review API at http://{args.host}:{args.port}")
    print(f"  OpenAPI docs: http://{args.host}:{args.port}/docs")
    print(f"  Corpus: {args.chunks}")
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
            summary = index_corpus_to_services(
                config,
                chunks,
                progress=lambda message: print(message, flush=True),
            )
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


def _cmd_backfill_entity_keys(_args: argparse.Namespace) -> int:
    from law_agent.config import load_service_config
    from law_agent.review.transfer_store import PostgresTransferStore

    count = PostgresTransferStore(load_service_config().postgres.dsn).backfill_entity_keys()
    print(f"Updated {count} entity keys")
    return 0


def _cmd_backfill_citation_index(args: argparse.Namespace) -> int:
    import psycopg

    from law_agent.config import load_service_config
    from law_agent.data.io import read_jsonl
    from law_agent.data.schemas import Chunk
    from law_agent.review.impact import citation_rows
    from law_agent.review.impact_store import PostgresImpactStore

    dsn = load_service_config().postgres.dsn
    records = PostgresImpactStore(dsn)
    chunks = {row.chunk_id: row for row in read_jsonl(Path(args.chunks), Chunk)}
    count = 0
    with psycopg.connect(dsn) as conn, conn.cursor(name="citation_backfill") as cur:
        cur.execute("SELECT id, case_id, result_json FROM review_tasks WHERE status = 'succeeded'")
        for task_id, case_id, result in cur:
            rows = list(citation_rows(task_id, case_id, result or {}, chunks))
            if not args.dry_run:
                records.index_citations(task_id, rows)
            count += len(rows)
    print(f"{'Would process' if args.dry_run else 'Processed'} {count} citations")
    return 0


def _cmd_impact_scan(args: argparse.Namespace) -> int:
    from law_agent.config import load_service_config
    from law_agent.review.dates import business_today
    from law_agent.review.impact_store import PostgresImpactStore

    count = PostgresImpactStore(load_service_config().postgres.dsn).promote(args.as_of or business_today())
    print(f"Promoted {count} regulation impact flags")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m law_agent.review")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("backfill-entity-keys").set_defaults(func=_cmd_backfill_entity_keys)
    citations = subparsers.add_parser("backfill-citation-index")
    citations.add_argument("--dry-run", action="store_true")
    citations.add_argument("--chunks", default=str(DEFAULT_CHUNKS_PATH))
    citations.set_defaults(func=_cmd_backfill_citation_index)
    impact_scan = subparsers.add_parser("impact-scan")
    impact_scan.add_argument("--as-of", type=date.fromisoformat)
    impact_scan.set_defaults(func=_cmd_impact_scan)

    serve = subparsers.add_parser("serve", help="Start the local FastAPI review API server")
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
