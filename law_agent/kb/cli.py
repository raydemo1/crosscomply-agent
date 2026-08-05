"""Interactive one-command CLI for knowledge-base ingestion."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from law_agent.config import require_service_config
from law_agent.data.chunking.pipeline import chunk_document
from law_agent.data.normalize import normalize_source
from law_agent.data.schemas import SourceRecord
from law_agent.kb.service import KnowledgeBase, processing_signature
from law_agent.kb.service_index import ServiceGenerationIndex
from law_agent.llm.embeddings import build_embeddings_provider

DEFAULT_CORPUS = Path("data/corpus/legal_docs_20260702")


def _infer_title(path: Path) -> str:
    if path.suffix.lower() in {".txt", ".md", ".markdown", ".html", ".htm"}:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            candidate = line.lstrip("# ").strip()
            if candidate:
                return candidate[:120]
    return path.stem.replace("_", " ")


def _provisional_source(path: Path) -> SourceRecord:
    return SourceRecord(
        source_id="candidate_" + hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12],
        title=_infer_title(path),
        source_url=path.resolve().as_uri(),
        source_site="local_import",
        doc_type="guideline",
        file_format=path.suffix.lstrip(".") or "txt",
        include_in_mvp=True,
    )


def _new_source_from_interaction(path: Path, title: str) -> SourceRecord:
    prompted_title = input(f"未找到可更新来源，标题 [{title}]：").strip() or title
    url = input("来源 URL（留空则记录本地文件）：").strip() or path.resolve().as_uri()
    content_digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
    prefix = hashlib.sha256(f"{prompted_title}\x1f{content_digest}".encode()).hexdigest()[:12]
    return SourceRecord(
        source_id=f"user_{prefix}",
        title=prompted_title,
        source_url=url,
        source_site="local_import" if url.startswith("file:") else Path(url).name or "manual",
        doc_type="guideline",
        file_format=path.suffix.lstrip(".") or "txt",
        include_in_mvp=True,
    )


def _load_metadata(path: Path) -> SourceRecord:
    return SourceRecord.model_validate(json.loads(path.read_text(encoding="utf-8")))


def _cmd_ingest(args: argparse.Namespace) -> int:
    file_path = Path(args.file)
    if not file_path.is_file():
        raise RuntimeError(f"文件不存在：{file_path}")
    provisional = _provisional_source(file_path)
    document = normalize_source(provisional, file_path, parser=args.parser)

    config = require_service_config()
    index = ServiceGenerationIndex(config)
    try:
        embeddings = build_embeddings_provider(config.embedding)
        kb = KnowledgeBase(
            Path(args.corpus),
            index=index,
            signature=processing_signature(
                embedding_model=config.embedding.model,
                embedding_dimension=config.embedding.dimension,
            ),
            embed_texts=embeddings.embed_texts,
        )
        exact = kb.exact_matches(document.text)
        if exact and not args.as_new:
            print("跳过重复内容：")
            for source in exact:
                print(f"  {source.title} ({source.source_id})")
            print("如需作为独立来源保留，请加 --as-new。")
            return 0

        if args.metadata:
            source = _load_metadata(Path(args.metadata))
        else:
            if args.non_interactive:
                raise RuntimeError("--non-interactive requires --metadata when the file is not a duplicate")
            candidates = kb.title_candidates(document.title)
            if candidates and not args.as_new:
                print("发现可能需要更正的来源：")
                for number, candidate in enumerate(candidates, start=1):
                    print(f"  {number}. {candidate.title} ({candidate.source_id})")
                answer = input("输入编号更新，直接回车作为新来源：").strip()
                if answer:
                    try:
                        source = candidates[int(answer) - 1]
                    except (ValueError, IndexError) as exc:
                        raise RuntimeError("候选编号无效，请重新执行导入命令") from exc
                else:
                    source = _new_source_from_interaction(file_path, document.title)
            else:
                source = _new_source_from_interaction(file_path, document.title)

        final_document = document.model_copy(
            update={
                "doc_id": source.source_id,
                "source_id": source.source_id,
                "title": source.title,
                "source_url": source.source_url,
                "source_site": source.source_site,
                "doc_type": source.doc_type,
                "authority": source.authority,
                "law_status": source.law_status,
                "publish_date": source.publish_date,
                "effective_date": source.effective_date,
                "issuing_body": source.issuing_body,
            }
        )
        chunks = chunk_document(final_document)
        result = kb.ingest_prepared(source, final_document.text, chunks, raw_file=file_path)
        print(
            f"{result.action}: {source.title} ({source.source_id}); "
            f"新增向量 {result.embedded_chunks}，缓存命中 {result.cached_chunks}。"
        )
    finally:
        index.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m law_agent.kb")
    subparsers = parser.add_subparsers(dest="command", required=True)
    ingest = subparsers.add_parser("ingest", help="导入文件并自动判断重复、新增或更正")
    ingest.add_argument("file")
    ingest.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    ingest.add_argument("--parser", choices=["auto", "plain", "docx", "docling", "mineru"], default="auto")
    ingest.add_argument("--as-new", action="store_true", help="即使正文相同也作为独立来源入库")
    ingest.add_argument("--metadata", help="批处理用 SourceRecord JSON；指定后不进入交互")
    ingest.add_argument("--non-interactive", action="store_true", help="拒绝交互；新增或更新时必须提供 --metadata")
    ingest.set_defaults(func=_cmd_ingest)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
