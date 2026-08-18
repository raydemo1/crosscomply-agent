"""Small argparse CLI for the data governance file pipeline."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from law_agent.config import require_llm_config
from law_agent.data.chunking.pipeline import chunk_document
from law_agent.data.cleaners.common import clean_text
from law_agent.data.cleaners.pipeline import clean_document
from law_agent.data.enrichment.generator import enrich_document
from law_agent.data.evalset.build_cases import build_retrieval_cases
from law_agent.data.fetchers.generic import fetch_source
from law_agent.data.io import read_jsonl, read_manifest, write_json, write_jsonl, write_manifest
from law_agent.data.manifest import build_manifest
from law_agent.data.normalize import normalize_source
from law_agent.data.reports.governance_report import build_data_governance_report
from law_agent.data.schemas import Chunk, CleanedDocument, Document, EnrichedDocument, SourceRecord


def _find_raw_path(raw_dir: Path, record: SourceRecord) -> Path:
    matches = [
        path
        for path in raw_dir.rglob(f"{record.source_id}.*")
        if not path.name.endswith(".meta.json") and path.is_file()
    ]
    if not matches:
        raise FileNotFoundError(f"No raw file found for {record.source_id} under {raw_dir}")
    expected_suffix = f".{record.file_format.lower().lstrip('.')}"
    for path in matches:
        if path.suffix.lower() == expected_suffix:
            return path
    return matches[0]


def _cmd_manifest_schema(args: argparse.Namespace) -> int:
    write_json(Path(args.output), SourceRecord.model_json_schema())
    print(f"Wrote schema to {args.output}")
    return 0


def _cmd_manifest_validate(args: argparse.Namespace) -> int:
    records = list(read_manifest(Path(args.path)))
    included = sum(1 for record in records if record.include_in_mvp)
    print(f"Validated {len(records)} sources ({included} included in MVP)")
    return 0


def _cmd_manifest_build(args: argparse.Namespace) -> int:
    records = build_manifest(
        args.topic,
        from_flk=args.from_flk,
        terms=args.term,
        limit=args.limit,
        timeout_seconds=args.timeout_seconds,
    )
    count = write_manifest(Path(args.output), records)
    print(f"Wrote {count} sources to {args.output}")
    return 0


def _cmd_config_check(args: argparse.Namespace) -> int:
    config = require_llm_config()
    print(f"LLM configured: base_url={config.base_url}, model={config.model}")
    return 0


def _cmd_fetch(args: argparse.Namespace) -> int:
    records = [record for record in read_manifest(Path(args.manifest)) if record.include_in_mvp]
    ok = 0
    for record in records:
        result = fetch_source(
            record,
            Path(args.output_dir),
            timeout_seconds=args.timeout_seconds,
            allow_network=True,
        )
        if not result.ok:
            raise RuntimeError(f"Failed to fetch {record.source_id}: {result.error}")
        ok += 1
    print(f"Fetched {ok} sources into {args.output_dir}")
    return 0


def _cmd_normalize(args: argparse.Namespace) -> int:
    manifest = read_manifest(Path(args.manifest))
    raw_dir = Path(args.raw_dir)
    parser_output_dir = Path(args.parser_output_dir) if args.parser_output_dir else None
    documents: list[Document] = []
    for record in manifest:
        if not record.include_in_mvp:
            continue
        documents.append(
            normalize_source(
                record,
                _find_raw_path(raw_dir, record),
                parser=args.parser,
                parser_output_dir=parser_output_dir,
            )
        )
    count = write_jsonl(Path(args.output), documents)
    print(f"Wrote {count} normalized documents to {args.output}")
    return 0


def _cmd_clean(args: argparse.Namespace) -> int:
    documents = read_jsonl(Path(args.input), Document)
    cleaned = [clean_document(document) for document in documents]
    count = write_jsonl(Path(args.output), cleaned)
    print(f"Wrote {count} cleaned documents to {args.output}")
    return 0


def _cmd_enrich(args: argparse.Namespace) -> int:
    documents = read_jsonl(Path(args.input), CleanedDocument)
    enriched = [enrich_document(document) for document in documents]
    count = write_jsonl(Path(args.output), enriched)
    print(f"Wrote {count} enriched documents to {args.output}")
    return 0


def _cmd_chunk(args: argparse.Namespace) -> int:
    documents = read_jsonl(Path(args.input), EnrichedDocument)
    chunks: list[Chunk] = []
    for document in documents:
        chunks.extend(chunk_document(document))
    count = write_jsonl(Path(args.output), chunks)
    print(f"Wrote {count} chunks to {args.output}")
    return 0


def _cmd_evalset_build(args: argparse.Namespace) -> int:
    chunks = read_jsonl(Path(args.chunks), Chunk)
    cases = build_retrieval_cases(chunks, limit=args.limit)
    count = write_jsonl(Path(args.output), cases)
    print(f"Wrote {count} retrieval cases to {args.output}")
    return 0


def _cmd_report_governance(args: argparse.Namespace) -> int:
    build_data_governance_report(
        documents=read_jsonl(Path(args.normalized), Document),
        cleaned=read_jsonl(Path(args.cleaned), CleanedDocument),
        enriched=read_jsonl(Path(args.enriched), EnrichedDocument),
        chunks=read_jsonl(Path(args.chunks), Chunk),
        output=Path(args.output),
    )
    print(f"Wrote report to {args.output}")
    return 0


def _cmd_pipeline_run(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest)
    raw_dir = Path(args.raw_dir)
    normalized_path = Path(args.normalized)
    cleaned_path = Path(args.cleaned)
    enriched_path = Path(args.enriched)
    chunks_path = Path(args.chunks)
    eval_path = Path(args.eval_output)
    report_path = Path(args.report_output)

    _cmd_fetch(
        argparse.Namespace(
            manifest=str(manifest_path),
            output_dir=str(raw_dir),
            timeout_seconds=args.timeout_seconds,
        )
    )
    _cmd_normalize(
        argparse.Namespace(
            manifest=str(manifest_path),
            raw_dir=str(raw_dir),
            output=str(normalized_path),
            parser=args.parser,
            parser_output_dir=args.parser_output_dir,
        )
    )
    _cmd_clean(argparse.Namespace(input=str(normalized_path), output=str(cleaned_path)))
    _cmd_enrich(argparse.Namespace(input=str(cleaned_path), output=str(enriched_path)))
    _cmd_chunk(argparse.Namespace(input=str(enriched_path), output=str(chunks_path)))
    _cmd_evalset_build(
        argparse.Namespace(chunks=str(chunks_path), output=str(eval_path), limit=args.limit)
    )
    _cmd_report_governance(
        argparse.Namespace(
            normalized=str(normalized_path),
            cleaned=str(cleaned_path),
            enriched=str(enriched_path),
            chunks=str(chunks_path),
            output=str(report_path),
        )
    )
    return 0


def _cmd_clean_text(args: argparse.Namespace) -> int:
    input_path = Path(args.input)
    output_path = Path(args.output)
    result = clean_text(input_path.read_text(encoding="utf-8"), title=args.title)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result.text, encoding="utf-8")
    print(json.dumps(result.rule_hits, ensure_ascii=False, sort_keys=True))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m law_agent.data")
    subparsers = parser.add_subparsers(dest="command")

    config = subparsers.add_parser("config", help="Check runtime configuration")
    config_sub = config.add_subparsers(dest="config_command")
    config_check = config_sub.add_parser("check", help="Validate required LLM configuration")
    config_check.set_defaults(func=_cmd_config_check)

    manifest = subparsers.add_parser("manifest", help="Work with source manifests")
    manifest_sub = manifest.add_subparsers(dest="manifest_command")

    manifest_build = manifest_sub.add_parser("build", help="Build a source manifest")
    manifest_build.add_argument("--topic", default="data_compliance")
    manifest_build.add_argument("--output", default="data/manifests/source_manifest.csv")
    manifest_build.add_argument("--from-flk", action="store_true", help="Search flk.npc.gov.cn")
    manifest_build.add_argument("--term", action="append", help="Restrict FLK search term")
    manifest_build.add_argument("--limit", type=int, default=None)
    manifest_build.add_argument("--timeout-seconds", type=int, default=30)
    manifest_build.set_defaults(func=_cmd_manifest_build)

    manifest_schema = manifest_sub.add_parser("schema", help="Write source manifest schema")
    manifest_schema.add_argument(
        "--output",
        default="data/manifests/source_manifest.schema.json",
        help="Path to write JSON schema",
    )
    manifest_schema.set_defaults(func=_cmd_manifest_schema)

    manifest_validate = manifest_sub.add_parser("validate", help="Validate a manifest CSV")
    manifest_validate.add_argument("path", help="Manifest CSV path")
    manifest_validate.set_defaults(func=_cmd_manifest_validate)

    clean = subparsers.add_parser("clean", help="Run cleaning helpers")
    clean_sub = clean.add_subparsers(dest="clean_command")
    clean_run = clean_sub.add_parser("run", help="Clean normalized documents")
    clean_run.add_argument("--input", default="data/normalized/documents.jsonl")
    clean_run.add_argument("--output", default="data/cleaned/documents.cleaned.jsonl")
    clean_run.set_defaults(func=_cmd_clean)

    clean_text_cmd = clean_sub.add_parser("text", help="Clean a UTF-8 text file")
    clean_text_cmd.add_argument("--input", required=True)
    clean_text_cmd.add_argument("--output", required=True)
    clean_text_cmd.add_argument("--title", default=None)
    clean_text_cmd.set_defaults(func=_cmd_clean_text)

    fetch = subparsers.add_parser("fetch", help="Fetch raw source materials")
    fetch.add_argument("--manifest", default="data/manifests/source_manifest.csv")
    fetch.add_argument("--output-dir", default="data/raw")
    fetch.add_argument("--timeout-seconds", type=int, default=30)
    fetch.set_defaults(func=_cmd_fetch)

    normalize = subparsers.add_parser("normalize", help="Normalize raw files to Document JSONL")
    normalize.add_argument("--manifest", default="data/manifests/source_manifest.csv")
    normalize.add_argument("--raw-dir", default="data/raw")
    normalize.add_argument("--output", default="data/normalized/documents.jsonl")
    normalize.add_argument(
        "--parser",
        choices=["auto", "plain", "docx", "docling", "mineru"],
        default="auto",
        help="Document parser engine. auto keeps FLK DOCX lightweight and uses Docling for PDF/image files.",
    )
    normalize.add_argument(
        "--parser-output-dir",
        default=None,
        help="Directory for parser artifacts such as MinerU markdown output.",
    )
    normalize.set_defaults(func=_cmd_normalize)

    enrich = subparsers.add_parser("enrich", help="Generate semantic enrichment")
    enrich.add_argument("--input", default="data/cleaned/documents.cleaned.jsonl")
    enrich.add_argument("--output", default="data/enriched/documents.enriched.jsonl")
    enrich.set_defaults(func=_cmd_enrich)

    chunk = subparsers.add_parser("chunk", help="Chunk enriched documents")
    chunk.add_argument("--input", default="data/enriched/documents.enriched.jsonl")
    chunk.add_argument("--output", default="data/chunks/chunks.jsonl")
    chunk.set_defaults(func=_cmd_chunk)

    evalset = subparsers.add_parser("evalset", help="Build evaluation candidates")
    evalset_sub = evalset.add_subparsers(dest="evalset_command")
    evalset_build = evalset_sub.add_parser("build", help="Build retrieval cases")
    evalset_build.add_argument("--chunks", default="data/chunks/chunks.jsonl")
    evalset_build.add_argument("--output", default="data/eval/retrieval_cases.jsonl")
    evalset_build.add_argument("--limit", type=int, default=60)
    evalset_build.set_defaults(func=_cmd_evalset_build)

    report = subparsers.add_parser("report", help="Build reports")
    report_sub = report.add_subparsers(dest="report_command")
    report_governance = report_sub.add_parser("governance", help="Build data governance report")
    report_governance.add_argument("--normalized", default="data/normalized/documents.jsonl")
    report_governance.add_argument("--cleaned", default="data/cleaned/documents.cleaned.jsonl")
    report_governance.add_argument("--enriched", default="data/enriched/documents.enriched.jsonl")
    report_governance.add_argument("--chunks", default="data/chunks/chunks.jsonl")
    report_governance.add_argument("--output", default="docs/data_governance_report.md")
    report_governance.set_defaults(func=_cmd_report_governance)

    pipeline = subparsers.add_parser("pipeline", help="Run the file pipeline")
    pipeline_sub = pipeline.add_subparsers(dest="pipeline_command")
    pipeline_run = pipeline_sub.add_parser("run", help="Run fetch through report")
    pipeline_run.add_argument("--manifest", default="data/manifests/source_manifest.csv")
    pipeline_run.add_argument("--raw-dir", default="data/raw")
    pipeline_run.add_argument("--normalized", default="data/normalized/documents.jsonl")
    pipeline_run.add_argument("--cleaned", default="data/cleaned/documents.cleaned.jsonl")
    pipeline_run.add_argument("--enriched", default="data/enriched/documents.enriched.jsonl")
    pipeline_run.add_argument("--chunks", default="data/chunks/chunks.jsonl")
    pipeline_run.add_argument("--eval-output", default="data/eval/retrieval_cases.jsonl")
    pipeline_run.add_argument("--report-output", default="docs/data_governance_report.md")
    pipeline_run.add_argument("--timeout-seconds", type=int, default=30)
    pipeline_run.add_argument("--limit", type=int, default=60)
    pipeline_run.add_argument(
        "--parser",
        choices=["auto", "plain", "docx", "docling", "mineru"],
        default="auto",
        help="Document parser engine passed to normalize.",
    )
    pipeline_run.add_argument(
        "--parser-output-dir",
        default=None,
        help="Directory for parser artifacts such as MinerU markdown output.",
    )
    pipeline_run.set_defaults(func=_cmd_pipeline_run)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args)
