"""Read-only inspection of the actual published chunk bodies and citation metadata."""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from law_agent.data.chunking.law import article_ordinal
from law_agent.data.citation_policy import can_cite_clause_chunk
from law_agent.data.io import read_jsonl, read_manifest
from law_agent.data.schemas import Chunk

DEFAULT_CORPUS = Path("data/corpus/legal_docs_20260702")
EXPLANATION_RE = re.compile(r"《[^》]+》\s*解读|现将有关情况解读如下")
CROSS_REFERENCE_RE = re.compile(
    r"^第[一二三四五六七八九十百千万零〇\d]+条第?"
    r"[一二三四五六七八九十百千万零〇\d]+[款项]"
)
MAX_CHUNK_CHARS = 650


def inspect(corpus: Path) -> dict:
    sources = {item.source_id: item for item in read_manifest(corpus / "source_manifest.csv")}
    grouped: dict[str, list[Chunk]] = defaultdict(list)
    seen_ids: set[str] = set()
    findings: list[dict] = []
    inventory: list[dict] = []

    def flag(code: str, chunk: Chunk, detail: str) -> None:
        findings.append({
            "code": code, "source_id": chunk.source_id,
            "chunk_id": chunk.chunk_id, "detail": detail,
        })

    for chunk in read_jsonl(corpus / "chunks.jsonl", Chunk):
        grouped[chunk.source_id].append(chunk)
        inventory.append({
            "source_id": chunk.source_id, "chunk_id": chunk.chunk_id,
            "index": chunk.chunk_index, "article_no": chunk.article_no,
            "can_cite_clause": chunk.can_cite_clause,
            "citation_role": chunk.citation_role,
            "heading_path": chunk.heading_path,
            "char_count": len(chunk.text), "text": chunk.text,
        })
        if chunk.chunk_id in seen_ids:
            flag("duplicate_chunk_id", chunk, "同一 chunk_id 重复出现")
        seen_ids.add(chunk.chunk_id)
        source = sources.get(chunk.source_id)
        if source is None:
            flag("unknown_source", chunk, "来源不在清单中")
        elif chunk.can_cite_clause != can_cite_clause_chunk(source, chunk.article_no):
            flag("citation_policy_mismatch", chunk, "已发布标记与来源引用策略不一致")
        if not chunk.text.strip():
            flag("empty_text", chunk, "正文为空")
        if len(chunk.text) > MAX_CHUNK_CHARS:
            flag("oversize", chunk, f"{len(chunk.text)} 字符")
        if chunk.can_cite_clause and EXPLANATION_RE.search(chunk.text):
            flag("explanation_citable", chunk, "条款 chunk 含法规解读标题或引言")
        if chunk.article_no and CROSS_REFERENCE_RE.match(chunk.text.strip()):
            flag("cross_reference_as_article", chunk, "交叉引用被标记成独立条款")

    for source_id, chunks in grouped.items():
        explanation_started = False
        last_article: int | None = None
        for index, chunk in enumerate(chunks):
            if chunk.can_cite_clause and EXPLANATION_RE.search(chunk.text):
                explanation_started = True
            elif explanation_started and chunk.can_cite_clause:
                flag("explanation_scope_citable", chunk,
                     "位于法规解读标题之后，却继续继承条款引用资格")
            if chunk.chunk_index != index:
                flag("index_gap", chunk, f"实际顺序 {index}，索引 {chunk.chunk_index}")
            expected_prev = chunks[index - 1].chunk_id if index else None
            expected_next = chunks[index + 1].chunk_id if index + 1 < len(chunks) else None
            if chunk.prev_chunk_id != expected_prev or chunk.next_chunk_id != expected_next:
                flag("broken_neighbors", chunk, "前后邻居与发布顺序不一致")
            article = article_ordinal(chunk.article_no) if chunk.article_no else None
            if article is not None and article != last_article:
                if last_article is not None and article != last_article + 1:
                    flag("article_order", chunk,
                         f"条号从 {last_article} 跳到 {article}，需核对是否误切交叉引用")
                last_article = article
        if source_id not in sources:
            continue
    for missing in sorted(set(sources) - set(grouped)):
        findings.append({"code": "source_without_chunks", "source_id": missing,
                         "chunk_id": None, "detail": "清单来源没有发布 chunk"})

    return {
        "audited_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "corpus": str(corpus), "source_count": len(sources),
        "chunk_count": len(inventory), "findings": findings,
        "sources_with_articles": sum(
            any(chunk.article_no for chunk in chunks) for chunks in grouped.values()
        ),
        "sources_without_articles": sorted(
            source_id for source_id, chunks in grouped.items()
            if not any(chunk.article_no for chunk in chunks)
        ),
        "inventory": inventory,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    report = inspect(args.corpus)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# 已发布 Chunk 结构审计", "",
        f"- 来源：{report['source_count']}；chunk：{report['chunk_count']}",
        f"- 含条款结构的来源：{report['sources_with_articles']}",
        f"- 无条款结构的来源：{len(report['sources_without_articles'])}",
        f"- 异常：{len(report['findings'])}", "", "## 异常", "",
    ]
    if report["findings"]:
        for item in report["findings"]:
            lines.append(f"- `{item['code']}` `{item['chunk_id'] or item['source_id']}`：{item['detail']}")
    else:
        lines.append("无。")
    lines.extend(["", "## 无条款结构来源", ""])
    lines.extend(f"- `{source_id}`" for source_id in report["sources_without_articles"])
    md_path = args.out.with_suffix(".md")
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"来源 {report['source_count']}；chunk {report['chunk_count']}；异常 {len(report['findings'])}")
    print(f"JSON: {args.out}\n汇总: {md_path}")
    return 1 if report["findings"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
