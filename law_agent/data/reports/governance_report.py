"""Markdown reports for data governance outputs."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from law_agent.data.io import ensure_parent
from law_agent.data.schemas import Chunk, CleanedDocument, Document, EnrichedDocument


def build_data_governance_report(
    documents: list[Document],
    cleaned: list[CleanedDocument],
    enriched: list[EnrichedDocument],
    chunks: list[Chunk],
    output: Path,
) -> None:
    doc_types = Counter(doc.doc_type for doc in documents)
    authorities = Counter(doc.authority for doc in documents)
    law_statuses = Counter(doc.law_status for doc in documents)
    issuing_bodies = Counter(doc.issuing_body or "unknown" for doc in documents)
    applicable_regions = Counter(doc.applicable_region for doc in documents)
    legal_domains = Counter(domain for doc in documents for domain in doc.legal_domain)
    applicable_subjects = Counter(
        subject for doc in documents for subject in doc.applicable_subjects
    )
    raw_formats = Counter(doc.raw_format for doc in documents)
    parser_versions = Counter(doc.ingest_meta.parser for doc in documents)
    article_chunks = sum(1 for chunk in chunks if chunk.article_no)
    paragraph_chunks = sum(1 for chunk in chunks if chunk.paragraph_no)
    item_chunks = sum(1 for chunk in chunks if chunk.item_no)
    table_chunks = sum(1 for chunk in chunks if "<table>" in chunk.text)
    heading_depths = Counter(len(chunk.heading_path) for chunk in chunks)
    rule_hits: Counter[str] = Counter()
    for doc in cleaned:
        rule_hits.update(doc.cleaning_rule_hits)

    lines = [
        "# 数据治理报告",
        "",
        "## 总览",
        "",
        f"- 规范化文档数：{len(documents)}",
        f"- 清洗文档数：{len(cleaned)}",
        f"- 语义增强文档数：{len(enriched)}",
        f"- Chunk 数：{len(chunks)}",
        "",
        "## 文档类型",
        "",
    ]
    lines.extend(f"- {key}: {value}" for key, value in sorted(doc_types.items()))
    lines.extend(["", "## 权威级别", ""])
    lines.extend(f"- {key}: {value}" for key, value in sorted(authorities.items()))
    lines.extend(["", "## 时效状态", ""])
    lines.extend(f"- {key}: {value}" for key, value in sorted(law_statuses.items()))
    lines.extend(["", "## 法律元数据", ""])
    lines.extend(f"- 发布机关 {key}: {value}" for key, value in sorted(issuing_bodies.items()))
    lines.extend(f"- 适用地域 {key}: {value}" for key, value in sorted(applicable_regions.items()))
    if legal_domains:
        lines.extend(f"- 法律领域 {key}: {value}" for key, value in sorted(legal_domains.items()))
    if applicable_subjects:
        lines.extend(
            f"- 适用对象 {key}: {value}" for key, value in sorted(applicable_subjects.items())
        )
    lines.extend(["", "## 解析统计", ""])
    lines.extend(f"- 原始格式 {key}: {value}" for key, value in sorted(raw_formats.items()))
    lines.extend(f"- 解析器 {key}: {value}" for key, value in sorted(parser_versions.items()))
    lines.extend(["", "## Chunk 结构", ""])
    lines.append(f"- 条文 chunk 数：{article_chunks}")
    lines.append(f"- 款级 chunk 数：{paragraph_chunks}")
    lines.append(f"- 项级独立 chunk 数：{item_chunks}")
    lines.append(f"- 含表格 chunk 数：{table_chunks}")
    lines.extend(
        f"- heading_path 深度 {key}: {value}" for key, value in sorted(heading_depths.items())
    )
    lines.extend(["", "## 清洗规则命中", ""])
    if rule_hits:
        lines.extend(f"- {key}: {value}" for key, value in sorted(rule_hits.items()))
    else:
        lines.append("- 暂无命中")
    lines.extend(["", "## 说明", "", "本报告由文件流水线生成，不包含真实原始数据。"])

    ensure_parent(output)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
