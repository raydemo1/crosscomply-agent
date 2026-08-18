"""Chunking pipeline dispatcher."""

from __future__ import annotations

from law_agent.data.chunking.law import chunk_law_document, split_law_article_sections
from law_agent.data.chunking.structured import chunk_structured_document
from law_agent.data.schemas import Chunk, Document

MIN_STANDALONE_CHUNK_CHARS = 20
MAX_MERGED_CHUNK_CHARS = 650


def chunk_document(document: Document) -> list[Chunk]:
    if document.doc_type in {"law", "regulation"}:
        return _normalize_tiny_chunks(document, chunk_law_document(document))
    if len(split_law_article_sections(document.text)) >= 3:
        return _normalize_tiny_chunks(document, chunk_law_document(document))
    return _normalize_tiny_chunks(document, chunk_structured_document(document))


def _normalize_tiny_chunks(document: Document, chunks: list[Chunk]) -> list[Chunk]:
    """Merge tiny heading/prefix chunks into adjacent text-bearing chunks."""

    merged: list[Chunk] = []
    index = 0
    while index < len(chunks):
        chunk = chunks[index]
        if chunk.char_count < MIN_STANDALONE_CHUNK_CHARS:
            if index + 1 < len(chunks):
                next_chunk = chunks[index + 1]
                merged_text = f"{chunk.text.strip()}\n{next_chunk.text.strip()}".strip()
                if len(merged_text) <= MAX_MERGED_CHUNK_CHARS:
                    merged.append(
                        chunk.model_copy(
                            update={
                                "text": merged_text,
                                "char_count": len(merged_text),
                                "next_chunk_id": next_chunk.next_chunk_id,
                            }
                        )
                    )
                    index += 2
                    continue
            if merged:
                previous = merged[-1]
                merged_text = f"{previous.text.strip()}\n{chunk.text.strip()}".strip()
                if len(merged_text) <= MAX_MERGED_CHUNK_CHARS:
                    merged[-1] = previous.model_copy(
                        update={
                            "text": merged_text,
                            "char_count": len(merged_text),
                            "next_chunk_id": chunk.next_chunk_id,
                        }
                    )
                    index += 1
                    continue
        merged.append(chunk)
        index += 1

    normalized: list[Chunk] = []
    for new_index, chunk in enumerate(merged):
        normalized.append(
            chunk.model_copy(
                update={
                    "chunk_id": f"{document.doc_id}:{new_index:04d}",
                    "chunk_index": new_index,
                    "prev_chunk_id": f"{document.doc_id}:{new_index - 1:04d}"
                    if new_index > 0
                    else None,
                    "next_chunk_id": (
                        f"{document.doc_id}:{new_index + 1:04d}"
                        if new_index + 1 < len(merged)
                        else None
                    ),
                }
            )
        )
    return normalized
