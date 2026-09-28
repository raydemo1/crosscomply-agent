"""Read-only audit of every corpus source through the live pipeline.

Re-parses, cleans, chunks and coverage-checks each manifest source against its
stored raw file, and reports the result next to what is currently published.

Nothing here writes to the knowledge base: no manifest, chunk, state or raw
file is touched, no embedding is requested and no search store is contacted.
The point is to see, before any repair, which sources the current code would
publish differently and which ones lose body text on the way in.

Every source is judged through ``prepare_source_for_ingest`` — the same
parse → clean → bind → gate → chunk → coverage boundary ingestion publishes
through — and the rebuild verdict is ``KnowledgeBase.is_up_to_date``, the same
duplicate rule ``ingest_prepared`` applies. A chunk-count or body-length delta
is reported for context only; it never decides whether a source is republished.

Any source the gates refuse, or that fails an audit with an unexpected error,
makes the run exit non-zero.

Usage::

    python scripts/audit_corpus_coverage.py --corpus data/corpus/legal_docs_20260702
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime
from html import unescape
from pathlib import Path

from law_agent.config import require_service_config
from law_agent.data.chunking.pipeline import (
    CHUNKING_VERSION,
    should_chunk_as_law,
)
from law_agent.data.cleaners.common import CLEANING_VERSION
from law_agent.data.io import read_jsonl, read_manifest, write_json
from law_agent.data.normalize import PARSER_PIPELINE_VERSION
from law_agent.data.schemas import Chunk, SourceRecord
from law_agent.kb.ingestion import (
    CoverageGap,
    ParseQualityError,
    prepare_source_for_ingest,
)
from law_agent.kb.service import (
    InMemoryIndex,
    KnowledgeBase,
    processing_signature,
)

DEFAULT_CORPUS = Path("data/corpus/legal_docs_20260702")
DEFAULT_REPORT_DIR = Path("data/review_runs")
WHITESPACE_RE = re.compile(r"\s+")
# A run of three or more single digits separated by spaces is the artefact a
# glyph-position parser leaves behind ("4 3 6 9 7"). It only exists as spacing,
# so it must be searched for in the text as published.
SPACED_DIGITS_RE = re.compile(r"\d(?:[ \u3000]\d){2,}")
_COMPARABLE_STRIP_RE = re.compile(r"[\s|#*]")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# How many of a blocked source's gaps the report spells out, largest first.
DIAGNOSTIC_GAP_LIMIT = 4

# Sources the remediation plan calls out by name. A standard number is checked
# in its clean form and for spacing corruption; a negative list is checked
# against concrete preface and list fragments so the verdict is about content,
# never about the parse route or a chunk count.
FOCUS_SOURCES: dict[str, dict[str, object]] = {
    "shanghai_free_trade_zone_data_export_negative_list_2025": {
        "label": "上海负面清单前言与清单正文",
        "fragments": [
            "数据出境负面清单是指根据",
            "关键信息基础设施运营者不适用本清单",
            "再保险领域",
            "需要通过数据出境安全评估的数据清单",
            "气象领域",
            "非表现中华人民共和国领域和中华人民共和国管辖的其他海域内气象情况的数据",
        ],
    },
    "beijing_free_trade_zone_data_export_negative_list_2025": {
        "label": "北京负面清单前言与清单正文",
        "fragments": [
            "数据出境管理清单（负面清单）",
            "汽车行业",
            "需要通过数据出境安全评估的数据清单",
            "银行行业",
            "本清单适用的银行行业企业主要包括",
        ],
    },
    "tc260_gbt_43697_2024_data_classification_rules": {
        "label": "标准编号",
        "number": "GB/T43697—2024",
    },
    "tc260_sensitive_pip_processing_requirements_2025": {
        "label": "标准编号",
        "number": "GB/T45574—2025",
    },
}


def _raw_path(raw_dir: Path, record: SourceRecord) -> Path | None:
    """Locate a source's stored raw file (``raw/{source_id}/source.{ext}``)."""

    def candidates(directory: Path) -> list[Path]:
        return [
            path
            for path in sorted(directory.glob("*"))
            if path.is_file() and not path.name.endswith(".meta.json")
        ]

    source_dir = raw_dir / record.source_id
    matches = candidates(source_dir) if source_dir.is_dir() else []
    if not matches:
        matches = [
            path
            for path in sorted(raw_dir.rglob(f"{record.source_id}.*"))
            if path.is_file() and not path.name.endswith(".meta.json")
        ]
    if not matches:
        return None
    for path in matches:
        if path.suffix.lower() == f".{record.file_format.lower().lstrip('.')}":
            return path
    return matches[0]


def _flat(text: str) -> str:
    return WHITESPACE_RE.sub("", text)


def _comparable(text: str) -> str:
    """Strip layout-only characters so two renderings of a phrase compare equal."""

    return _COMPARABLE_STRIP_RE.sub("", _HTML_TAG_RE.sub("", unescape(text)))


def _chunks_fingerprint(chunks: list[Chunk]) -> str:
    material = "\x1f".join(chunk.text for chunk in chunks)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]


def _focus_checks(body: str, chunks: list[Chunk], focus: dict[str, object]) -> dict[str, object]:
    """Check the exact text each focus source was reported to lose."""

    chunks_raw = "".join(chunk.text for chunk in chunks)
    checks: dict[str, object] = {"expectation": focus["label"]}
    if "number" in focus:
        number = str(focus["number"])
        checks["expected_number"] = number
        checks["number_in_body"] = number in _flat(body)
        checks["number_in_chunks"] = number in _flat(chunks_raw)
        # Looked for in the published text, not a flattened copy: flattening
        # removes exactly the spaces this check exists to detect.
        checks["spaced_digits_in_body"] = SPACED_DIGITS_RE.findall(body)[:5]
        checks["spaced_digits_in_chunks"] = SPACED_DIGITS_RE.findall(chunks_raw)[:5]
        return checks

    body_comparable = _comparable(body)
    chunks_comparable = _comparable(chunks_raw)
    fragments = []
    for fragment in focus["fragments"]:  # type: ignore[union-attr]
        needle = _comparable(str(fragment))
        fragments.append(
            {
                "text": fragment,
                "in_body": needle in body_comparable,
                "in_chunks": needle in chunks_comparable,
            }
        )
    checks["fragments"] = fragments
    checks["missing_in_body"] = [item["text"] for item in fragments if not item["in_body"]]
    checks["missing_in_chunks"] = [item["text"] for item in fragments if not item["in_chunks"]]
    return checks


def _focus_anomaly(checks: dict[str, object]) -> bool:
    """Whether a focus source lost text the plan says it must carry.

    Reaching the cleaned body is not the verdict; reaching the index is. A
    fragment present in the body but absent from every chunk is exactly the
    loss this audit exists to catch, so both are required.
    """

    if "expected_number" in checks:
        return not checks["number_in_chunks"] or bool(checks["spaced_digits_in_chunks"])
    return bool(checks["missing_in_body"]) or bool(checks["missing_in_chunks"])


def _diagnostic_gaps(gaps: list[CoverageGap]) -> list[CoverageGap]:
    """The first gap plus the largest ones.

    A blocked source must be classifiable from the report alone: layout
    repetition shows up as many small gaps, real body loss as one big one. The
    full list can hold thousands of entries, so only the first gap in document
    order and the largest few are written out.
    """

    selected: list[CoverageGap] = []
    for gap in [*gaps[:1], *sorted(gaps, key=lambda item: -item.chars)[:DIAGNOSTIC_GAP_LIMIT]]:
        if gap not in selected:
            selected.append(gap)
    return selected


def _audit_source(
    record: SourceRecord, corpus: Path, old_chunks: list[Chunk], kb: KnowledgeBase
) -> dict[str, object]:
    raw_path = _raw_path(corpus / "raw", record)
    result: dict[str, object] = {
        "source_id": record.source_id,
        "title": record.title,
        "doc_type": record.doc_type,
        "authority": record.authority,
        "citation_role": record.citation_role,
        "law_status": record.law_status,
        "effective_date": record.effective_date,
        "file_format": record.file_format,
        "include_in_mvp": record.include_in_mvp,
        "blocks_publish": False,
        "needs_republish": False,
    }
    if raw_path is None:
        result["error"] = "raw 文件缺失"
        result["blocks_publish"] = True
        return result
    result["raw"] = str(raw_path.relative_to(corpus))

    try:
        prepared = prepare_source_for_ingest(record, raw_path)
    except ParseQualityError as exc:
        result["error"] = f"document_gate: {exc}"
        result["blocks_publish"] = True
        return result
    except Exception as exc:  # noqa: BLE001 - one bad source must not stop the audit
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["blocks_publish"] = True
        return result

    document = prepared.document
    chunks = prepared.chunks
    coverage = prepared.coverage
    old_fingerprint = _chunks_fingerprint(old_chunks)
    new_fingerprint = _chunks_fingerprint(chunks)
    old_published = _flat("".join(chunk.text for chunk in old_chunks))
    new_body = _flat(document.text)
    body_ratio = round(len(new_body) / len(old_published), 4) if old_published else None
    publishable = not prepared.blocks_publish

    result.update(
        {
            "parser": document.ingest_meta.parser,
            "parser_version": document.ingest_meta.parser_version,
            "cleaning_rule_hits": document.cleaning_rule_hits,
            "strategy": document.ingest_meta.strategy,
            "ocr_used": document.ingest_meta.ocr_used,
            "document_gate": document.ingest_meta.quality_status,
            "chunk_route": "law" if should_chunk_as_law(document) else "structured",
            "body_chars": len(document.text),
            "body_ws": len(new_body),
            "old_published_ws": len(old_published),
            "body_ws_ratio_vs_old": body_ratio,
            "old_chunk_count": len(old_chunks),
            "new_chunk_count": len(chunks),
            "new_chunk_chars": sum(chunk.char_count for chunk in chunks),
            "article_nos": sum(1 for chunk in chunks if chunk.article_no),
            "old_chunks_fingerprint": old_fingerprint,
            "new_chunks_fingerprint": new_fingerprint,
            "chunks_unchanged": old_fingerprint == new_fingerprint,
            "chunk_quality": {
                "status": prepared.chunk_quality.status,
                "issues": prepared.chunk_quality.counts(),
            },
            "coverage": {
                "ratio": round(coverage.ratio, 4),
                "lines": f"{coverage.covered_lines}/{coverage.total_lines}",
                "char_count": coverage.char_count,
                "missing_chars": coverage.missing_chars,
                "largest_gap_chars": coverage.largest_gap_chars,
                "excluded": coverage.excluded,
                "blocks_publish": coverage.blocks_publish,
                "gap_count": len(coverage.missing),
                "gaps": [
                    {"line_no": gap.line_no, "chars": gap.chars, "text": gap.text[:400]}
                    for gap in _diagnostic_gaps(coverage.missing)
                ],
            },
            "blocks_publish": prepared.blocks_publish,
            # The rebuild verdict is the real ingest decision, never a proxy:
            # a source whose body is unchanged but whose chunking, processing
            # signature or source metadata moved is not current.
            "needs_republish": publishable
            and not kb.is_up_to_date(record, document.text, chunks),
        }
    )
    focus = FOCUS_SOURCES.get(record.source_id)
    if focus is not None:
        checks = _focus_checks(document.text, chunks, focus)
        result["focus_checks"] = checks
        if _focus_anomaly(checks):
            result["focus_anomaly"] = True
    return result


def _summarize(report: dict[str, object], sources: list[dict[str, object]]) -> str:
    lines: list[str] = [
        f"# 全库只读预检（{report['audited_at']}）",
        "",
        f"- 来源：{len(sources)}（清单 {report['manifest_count']} 条）",
        f"- 解析管线：{report['versions']['parser']}",
        f"- 清洗版本：{report['versions']['cleaning']} / 切分版本：{report['versions']['chunking']}",
        (
            f"- 签名：已发布 {report['signature']['published']} -> 当前"
            f" {report['signature']['current']}"
            f"（{'变化，全部缓存向量失效' if report['signature']['changed'] else '未变化'}）"
        ),
        "",
        "## 结论",
        "",
    ]
    errors = [item for item in sources if item.get("error")]
    blocking = [item for item in sources if item.get("blocks_publish")]
    focus_anomalies = [item for item in sources if item.get("focus_anomaly")]
    republish = [item for item in sources if item.get("needs_republish")]
    routes = Counter(str(item.get("chunk_route")) for item in sources if not item.get("error"))
    lines.append(f"- 审计失败：{len(errors)}")
    lines.append(f"- 阻断发布（解析/覆盖门禁）：{len(blocking)}")
    lines.append(f"- 重点来源异常：{len(focus_anomalies)}")
    lines.append(f"- 建议重新发布：{len(republish)}")
    lines.append(f"- 切分路由：law={routes['law']} structured={routes['structured']}")
    lines.append("")

    if errors:
        lines.append("## 门禁阻断")
        lines.append("")
        for item in sources:
            if not item.get("blocks_publish"):
                continue
            if item.get("error"):
                lines.append(f"- {item['source_id']}：{item['error']}")
                continue
            coverage = item["coverage"]
            lines.append(
                f"- {item['source_id']}：正文 {coverage['lines']} 行进入 Chunk，"
                f"缺失 {coverage['missing_chars']} 字符，{coverage['gap_count']} 处缺口，"
                f"最大缺口 {coverage['largest_gap_chars']} 字符，"
                f"排版性排除 {coverage['excluded']}"
            )
        lines.append("")

    if focus_anomalies:
        lines.append("## 重点来源异常")
        lines.append("")
        for item in focus_anomalies:
            lines.append(f"- {item['source_id']}：`{json.dumps(item['focus_checks'], ensure_ascii=False)}`")
        lines.append("")

    lines.append("## 全部来源")
    lines.append("")
    lines.append(
        "| source | route | old→new chunks | lines | missing | largest_gap | republish |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for item in sorted(sources, key=lambda entry: str(entry.get("source_id"))):
        if item.get("error"):
            lines.append(f"| {item['source_id']} | 阻断 |  |  |  |  |  |")
            continue
        coverage = item["coverage"]
        lines.append(
            f"| {item['source_id']} | {item['chunk_route']}"
            f" | {item['old_chunk_count']}→{item['new_chunk_count']}"
            f" | {coverage['lines']} | {coverage['missing_chars']}"
            f" | {coverage['largest_gap_chars']}"
            f" | {'是' if item['needs_republish'] else '否'} |"
        )
    lines.append("")

    focus = [item for item in sources if item.get("focus_checks")]
    if focus:
        lines.append("## 重点来源核验")
        lines.append("")
        for item in focus:
            lines.append(f"### {item['source_id']}")
            lines.append("")
            lines.append(f"- 路由：{item['chunk_route']}，chunk {item['old_chunk_count']}→{item['new_chunk_count']}")
            lines.append(
                f"- 覆盖：{item['coverage']['lines']}，缺失 {item['coverage']['missing_chars']} 字符"
                f"，最大缺口 {item['coverage']['largest_gap_chars']}"
            )
            lines.append(f"- 核验：`{json.dumps(item['focus_checks'], ensure_ascii=False)}`")
            if item["coverage"]["gaps"]:
                for gap in item["coverage"]["gaps"]:
                    lines.append(f"  - L{gap['line_no']}（{gap['chars']} 字符）{gap['text'][:100]}")
            lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="audit_corpus_coverage.py")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    args = parser.parse_args(argv)

    manifest_path = args.corpus / "source_manifest.csv"
    records = list(read_manifest(manifest_path))
    old_by_source: dict[str, list[Chunk]] = {}
    chunks_path = args.corpus / "chunks.jsonl"
    if chunks_path.exists():
        for chunk in read_jsonl(chunks_path, Chunk):
            old_by_source.setdefault(chunk.source_id, []).append(chunk)

    published_signature = None
    state_path = args.corpus / ".knowledge_base_state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        signatures = {entry.get("signature") for entry in state["sources"].values()}
        published_signature = signatures.pop() if len(signatures) == 1 else sorted(signatures)

    config = require_service_config()
    signature = processing_signature(
        embedding_model=config.embedding.model,
        embedding_dimension=config.embedding.dimension,
    )
    kb = KnowledgeBase(args.corpus, index=InMemoryIndex(), signature=signature)

    args.report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
    progress_path = args.report_dir / f"corpus_coverage_audit_{stamp}.jsonl"
    sources = []
    with progress_path.open("w", encoding="utf-8", newline="\n") as progress:
        for record in records:
            item = _audit_source(record, args.corpus,
                                 old_by_source.get(record.source_id, []), kb)
            sources.append(item)
            progress.write(json.dumps(item, ensure_ascii=False) + "\n")
            progress.flush()
            os.fsync(progress.fileno())

    new_chunks_total = sum(int(item.get("new_chunk_count") or 0) for item in sources)
    republish = [item for item in sources if item.get("needs_republish")]
    republish_chunks = sum(int(item["new_chunk_count"]) for item in republish)
    blocking = [item for item in sources if item.get("blocks_publish")]
    errors = [item for item in sources if item.get("error")]
    focus_anomalies = [item for item in sources if item.get("focus_anomaly")]
    report = {
        "audited_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "corpus": str(args.corpus),
        "manifest_count": len(records),
        "versions": {
            "parser": PARSER_PIPELINE_VERSION,
            "cleaning": CLEANING_VERSION,
            "chunking": CHUNKING_VERSION,
        },
        "signature": {
            "published": published_signature,
            "current": signature,
            "changed": published_signature != signature,
        },
        "rebuild": {
            "republish_sources": len(republish),
            "republish_chunks": republish_chunks,
            "blocked_sources": len(blocking),
            "audit_errors": len(errors),
            "focus_anomalies": len(focus_anomalies),
            "note": (
                "republish_chunks 才是需要重新计算向量的准确工作量的下限；"
                "签名变化时缓存键全部失效，缓存命中数为 0，实际调用数等于各来源重发 chunk 数之和。"
            ),
        },
        "new_chunks_total_all_sources": new_chunks_total,
        "sources": sources,
    }
    if state_path.exists():
        report["rebuild"]["cached_vectors_in_state"] = len(state["embedding_cache"])

    json_path = args.report_dir / f"corpus_coverage_audit_{stamp}.json"
    summary_path = args.report_dir / f"corpus_coverage_audit_{stamp}.md"
    write_json(json_path, report)
    summary = _summarize(report, sources)
    summary_path.write_text(summary + "\n", encoding="utf-8")
    print(summary)
    print(f"\nJSON: {json_path}\n汇总: {summary_path}")

    anomalies = len(blocking) + len(focus_anomalies)
    if anomalies:
        print(f"\n发现 {anomalies} 处异常：{len(errors)} 个审计错误，"
              f"{len(blocking)} 个阻断发布，{len(focus_anomalies)} 个重点来源异常。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
