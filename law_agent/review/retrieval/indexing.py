"""Index artifact builders for service-backed retrieval."""

from __future__ import annotations

import json
from pathlib import Path

from law_agent.data.schemas import Chunk
from law_agent.review.retrieval.corpus import load_corpus


def chunk_index_document(
    chunk: Chunk,
    *,
    generation_id: str | None = None,
    retrieval_enabled: bool = True,
) -> dict[str, object]:
    """Return a stable service-index document for one chunk."""

    # ``chunk_id`` is the stable logical identity used by citations and the
    # embedding cache. ``index_id`` is generation-scoped so a new generation
    # can be staged without overwriting an unchanged current chunk.
    index_id = f"{generation_id}:{chunk.chunk_id}" if generation_id else chunk.chunk_id
    document: dict[str, object] = {
        "index_id": index_id,
        "chunk_id": chunk.chunk_id,
        "doc_id": chunk.doc_id,
        "source_id": chunk.source_id,
        "title": chunk.title,
        "text": chunk.text,
        "chunk_index": chunk.chunk_index,
        "doc_type": chunk.doc_type,
        "heading_path": chunk.heading_path,
        "article_no": chunk.article_no,
        "paragraph_no": chunk.paragraph_no,
        "item_no": chunk.item_no,
        "citation_label": chunk.citation_label,
        "citation_role": chunk.citation_role,
        "can_cite_clause": chunk.can_cite_clause,
        "prev_chunk_id": chunk.prev_chunk_id,
        "next_chunk_id": chunk.next_chunk_id,
        "authority": chunk.authority,
        "law_status": chunk.law_status,
        "publish_date": chunk.publish_date,
        "effective_date": chunk.effective_date,
        "source_url": chunk.source_url,
        "applicable_region": chunk.applicable_region,
        "issuing_body": chunk.issuing_body,
        "legal_domain": chunk.legal_domain,
        "applicable_subjects": chunk.applicable_subjects,
        "topic_tags": chunk.topic_tags,
        "char_count": chunk.char_count,
    }
    document["generation_id"] = generation_id
    document["retrieval_enabled"] = retrieval_enabled
    return document


def build_elasticsearch_bulk_lines(
    chunks: list[Chunk],
    *,
    index_name: str,
) -> list[str]:
    """Build Elasticsearch bulk API NDJSON lines."""

    lines: list[str] = []
    for chunk in chunks:
        document = chunk_index_document(chunk)
        lines.append(
            json.dumps(
                {"index": {"_index": index_name, "_id": document["index_id"]}},
                ensure_ascii=False,
            )
        )
        lines.append(json.dumps(document, ensure_ascii=False))
    return lines


def build_pgvector_rows(
    chunks: list[Chunk],
    *,
    embeddings: dict[str, list[float]] | None = None,
) -> list[dict[str, object]]:
    """Build rows for PostgreSQL metadata + pgvector indexing."""

    vectors = embeddings or {}
    rows: list[dict[str, object]] = []
    for chunk in chunks:
        doc = chunk_index_document(chunk)
        doc["embedding"] = vectors.get(chunk.chunk_id)
        rows.append(doc)
    return rows


def write_elasticsearch_bulk_file(
    *,
    chunks_path: Path | str,
    output_path: Path | str,
    index_name: str,
) -> Path:
    """Write Elasticsearch bulk NDJSON for a chunks file."""

    chunks = load_corpus(chunks_path)
    lines = build_elasticsearch_bulk_lines(chunks, index_name=index_name)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def write_pgvector_rows_file(
    *,
    chunks_path: Path | str,
    output_path: Path | str,
    embeddings: dict[str, list[float]] | None = None,
) -> Path:
    """Write JSONL rows suitable for PostgreSQL/pgvector import."""

    chunks = load_corpus(chunks_path)
    rows = build_pgvector_rows(chunks, embeddings=embeddings)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    return path
