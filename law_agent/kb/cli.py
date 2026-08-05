"""Interactive one-command CLI for knowledge-base ingestion."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import unicodedata
from pathlib import Path

from law_agent.config import require_service_config
from law_agent.data.chunking.pipeline import chunk_document
from law_agent.data.cleaners.pipeline import clean_document
from law_agent.data.normalize import normalize_source
from law_agent.data.schemas import SourceRecord
from law_agent.kb.service import InMemoryIndex, KnowledgeBase, SourceSummary, processing_signature
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


def _prepare_document_for_ingest(path: Path, *, parser: str):
    """Run the mandatory parse-and-clean part of every ingest request."""

    provisional = _provisional_source(path)
    return clean_document(normalize_source(provisional, path, parser=parser))


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


def _display_width(value: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in value)


def _fit(value: object, width: int, *, align: str = "left") -> str:
    text = str(value)
    if _display_width(text) > width:
        ellipsis_width = _display_width("…")
        kept: list[str] = []
        used = 0
        for char in text:
            char_width = _display_width(char)
            if used + char_width + ellipsis_width > width:
                break
            kept.append(char)
            used += char_width
        text = "".join(kept) + "…"
    padding = width - _display_width(text)
    return f"{' ' * padding}{text}" if align == "right" else f"{text}{' ' * padding}"


def _print_sources(summaries: list[SourceSummary]) -> None:
    if not summaries:
        print("知识库目前没有资料。")
        return
    columns = [
        ("编号", 4, "right"),
        ("标题", 42, "left"),
        ("类型", 12, "left"),
        ("格式", 6, "left"),
        ("Chunk", 7, "right"),
        ("状态", 8, "left"),
    ]

    def row(values: list[object]) -> str:
        cells = [
            _fit(value, width, align=align)
            for value, (_header, width, align) in zip(values, columns, strict=True)
        ]
        return "| " + " | ".join(cells) + " |"

    divider = "+-" + "-+-".join("-" * width for _header, width, _align in columns) + "-+"
    print(f"现有知识库资料（{len(summaries)} 份）：")
    print(divider)
    print(row([header for header, _width, _align in columns]))
    print(divider)
    for number, summary in enumerate(summaries, start=1):
        source = summary.source
        print(row([number, source.title, source.doc_type, summary.raw_format, summary.chunk_count, summary.status]))
    print(divider)


def _resolve_source_target(target: str, summaries: list[SourceSummary]) -> SourceSummary:
    by_id = {summary.source.source_id: summary for summary in summaries}
    if target in by_id:
        return by_id[target]
    if target.isdigit():
        number = int(target)
        if 1 <= number <= len(summaries):
            return summaries[number - 1]
    raise RuntimeError("未找到该来源，请使用 list 显示的编号或来源 ID")


def _cmd_list(args: argparse.Namespace) -> int:
    kb = KnowledgeBase(Path(args.corpus), index=InMemoryIndex())
    _print_sources(kb.list_sources())
    return 0


def _cmd_remove(args: argparse.Namespace) -> int:
    corpus = Path(args.corpus)
    read_only_kb = KnowledgeBase(corpus, index=InMemoryIndex())
    summaries = read_only_kb.list_sources()
    _print_sources(summaries)
    if not summaries:
        return 0

    target = args.target or input("输入要删除的编号或来源 ID：").strip()
    if not target:
        raise RuntimeError("未选择要删除的来源")
    selected = _resolve_source_target(target, summaries)
    source = selected.source
    print(
        f"\n将永久删除：{source.title} ({source.source_id})\n"
        f"  影响：{selected.chunk_count} 个 Chunk、该来源 raw 文件、当前语料记录、ES 和 pgvector 索引。"
    )
    if not args.yes:
        answer = input(f"输入完整来源 ID 以确认删除 [{source.source_id}]：").strip()
        if answer != source.source_id:
            print("已取消，未删除任何资料。")
            return 0

    config = require_service_config()
    index = ServiceGenerationIndex(config)
    try:
        kb = KnowledgeBase(corpus, index=index)
        removed = kb.remove_source(source.source_id)
    finally:
        index.close()
    print(f"已删除：{removed.source.title}；移除 {removed.chunk_count} 个 Chunk。")
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    file_path = Path(args.file)
    if not file_path.is_file():
        raise RuntimeError(f"文件不存在：{file_path}")
    # New material always follows the same canonical path before identity,
    # chunking and embedding: parse -> deterministic clean -> chunk.
    document = _prepare_document_for_ingest(file_path, parser=args.parser)

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

    list_sources = subparsers.add_parser("list", help="列出当前知识库资料")
    list_sources.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    list_sources.set_defaults(func=_cmd_list)

    remove = subparsers.add_parser("remove", help="永久删除指定知识库资料")
    remove.add_argument("target", nargs="?", help="list 显示的编号或来源 ID；不填则交互选择")
    remove.add_argument("--corpus", default=str(DEFAULT_CORPUS))
    remove.add_argument("--yes", action="store_true", help="跳过确认，仅供脚本调用")
    remove.set_defaults(func=_cmd_remove)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (RuntimeError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
