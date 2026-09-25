"""Apply a reviewed corpus-maintenance plan to one knowledge base.

The plan is a JSON list of operations. Each operation is one of:

* ``refresh`` -- republish one source's stored chunks under corrected metadata.
  A provenance repair (official URL, dates, law status, citation role) does not
  change the body: chunk identity, stored content hash and embeddings stay as
  they are.
* ``replace`` -- re-parse the source's stored raw file and republish its body,
  keeping the same source identity. Use only when the official text really
  changed.
* ``import``  -- ingest a new source from a local raw file.
* ``remove``  -- retire a source from the active corpus and retrieval stores.

Run with ``--apply`` to write; the default is a read-only preview.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from law_agent.config import require_service_config
from law_agent.data.io import read_manifest
from law_agent.data.schemas import SourceRecord
from law_agent.kb.ingestion import prepare_chunks_for_publish, prepare_document_for_ingest
from law_agent.kb.service import InMemoryIndex, KnowledgeBase, processing_signature
from law_agent.kb.service_index import ServiceGenerationIndex
from law_agent.llm.embeddings import build_embeddings_provider

DEFAULT_CORPUS = Path("data/corpus/legal_docs_20260702")
OPERATIONS = {"refresh", "replace", "import", "remove"}


def _existing_sources(corpus: Path) -> dict[str, SourceRecord]:
    manifest = corpus / "source_manifest.csv"
    if not manifest.exists():
        return {}
    return {record.source_id: record for record in read_manifest(manifest)}


def _plan_source(entry: dict, existing: dict[str, SourceRecord]) -> SourceRecord:
    operation = entry["op"]
    if operation == "import":
        return SourceRecord.model_validate(entry["source"])
    source_id = entry["source_id"]
    current = existing.get(source_id)
    if current is None:
        raise RuntimeError(f"清单中没有来源：{source_id}")
    changes = entry.get("set") or {}
    unknown = set(changes) - set(SourceRecord.model_fields)
    if unknown:
        raise RuntimeError(f"未知字段 {sorted(unknown)}（来源 {source_id}）")
    return current.model_copy(update=changes)


def _describe(operation: str, source: SourceRecord, changes: dict) -> str:
    if operation == "refresh":
        rendered = ", ".join(f"{key}={value!r}" for key, value in sorted(changes.items()))
        return f"refresh {source.source_id}: {rendered}"
    if operation == "replace":
        return f"replace {source.source_id}（重新解析 raw 并复用 source 身份）"
    if operation == "remove":
        return f"remove {source.source_id}: {source.title}"
    return f"import {source.source_id}: {source.title}"


def _run_operation(
    kb: KnowledgeBase,
    entry: dict,
    source: SourceRecord,
    *,
    dry_run: bool,
) -> str:
    operation = entry["op"]
    if operation == "remove":
        if dry_run:
            return "将删除该来源及其索引"
        removed = kb.remove_source(source.source_id)
        return f"已删除 {removed.chunk_count} 个 chunk"
    if operation == "refresh":
        if dry_run:
            return "将只重发元数据（正文、chunk 身份与向量保持不变）"
        result = kb.update_source_metadata(source)
        return (
            f"{result.action}: 新增向量 {result.embedded_chunks}，缓存命中 {result.cached_chunks}"
        )

    raw_path = Path(entry["raw"])
    if not raw_path.is_file():
        raise RuntimeError(f"raw 文件不存在：{raw_path}")
    if dry_run:
        return f"将解析 {raw_path.name} 并重发"
    document = prepare_document_for_ingest(raw_path, parser=entry.get("parser", "auto"))
    duplicate_ids = sorted(
        other.source_id
        for other in kb.exact_matches(document.text)
        if other.source_id != source.source_id
    )
    if duplicate_ids:
        print(f"    警告：正文与以下来源相同 -> {', '.join(duplicate_ids)}")
    final_document = document.model_copy(
        update={
            "doc_id": source.source_id,
            "source_id": source.source_id,
            "library_kind": source.library_kind,
            "title": source.title,
            "source_url": source.source_url,
            "source_site": source.source_site,
            "doc_type": source.doc_type,
            "authority": source.authority,
            "citation_role": source.citation_role,
            "law_status": source.law_status,
            "publish_date": source.publish_date,
            "effective_date": source.effective_date,
            "issuing_body": source.issuing_body,
            "owning_department": source.owning_department,
            "internal_status": source.internal_status,
            "applicable_region": source.applicable_region,
            "legal_domain": source.legal_domain,
            "applicable_subjects": source.applicable_subjects,
            "topic_tags": source.topic_tags,
        }
    )
    chunks = prepare_chunks_for_publish(final_document)
    result = kb.ingest_prepared(
        source,
        final_document.text,
        chunks,
        raw_file=raw_path,
    )
    return f"{result.action}: 新增向量 {result.embedded_chunks}，缓存命中 {result.cached_chunks}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="refresh_corpus_sources.py")
    parser.add_argument("plan", type=Path, help="JSON 计划文件")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--apply", action="store_true", help="真正写入；缺省只预览")
    args = parser.parse_args(argv)

    plan = json.loads(args.plan.read_text(encoding="utf-8"))
    if not isinstance(plan, list) or not plan:
        raise RuntimeError("计划文件必须是非空 JSON 数组")
    for entry in plan:
        operation = entry.get("op")
        if operation not in OPERATIONS:
            raise RuntimeError(f"未知操作：{operation!r}")

    existing = _existing_sources(args.corpus)
    prepared = [(entry, _plan_source(entry, existing)) for entry in plan]
    index = InMemoryIndex() if not args.apply else None
    config = None
    if args.apply:
        config = require_service_config()
        index = ServiceGenerationIndex(config)
    try:
        kb = (
            KnowledgeBase(args.corpus, index=index)
            if config is None
            else KnowledgeBase(
                args.corpus,
                index=index,
                signature=processing_signature(
                    embedding_model=config.embedding.model,
                    embedding_dimension=config.embedding.dimension,
                ),
                embed_texts=build_embeddings_provider(config.embedding).embed_texts,
            )
        )
        for entry, source in prepared:
            print(f"[{entry['op']}] {_describe(entry['op'], source, entry.get('set') or {})}")
            outcome = _run_operation(kb, entry, source, dry_run=not args.apply)
            print(f"    {outcome}")
    finally:
        if args.apply:
            index.close()
    if not args.apply:
        print("\n预览完成：未写入任何内容。加 --apply 才会生效。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
