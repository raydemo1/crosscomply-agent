"""Chunking pipeline dispatcher."""

from __future__ import annotations

from law_agent.data.chunking.law import chunk_law_document, split_law_article_sections
from law_agent.data.chunking.structured import chunk_structured_document
from law_agent.data.citation_policy import can_cite_clause_chunk, citation_role_for_source
from law_agent.data.schemas import Chunk, Document, SourceRecord

MIN_STANDALONE_CHUNK_CHARS = 20
MAX_MERGED_CHUNK_CHARS = 650


def chunk_document(document: Document) -> list[Chunk]:
    if document.doc_type in {"law", "regulation"}:
        chunks = _normalize_tiny_chunks(document, chunk_law_document(document))
        return [chunk.model_copy(update={"valid_to": document.valid_to, "instrument_key": document.instrument_key}) for chunk in chunks]
    if len(split_law_article_sections(document.text)) >= 3:
        chunks = _normalize_tiny_chunks(document, chunk_law_document(document))
        return [chunk.model_copy(update={"valid_to": document.valid_to, "instrument_key": document.instrument_key}) for chunk in chunks]
    chunks = _normalize_tiny_chunks(document, chunk_structured_document(document))
    return [chunk.model_copy(update={"valid_to": document.valid_to, "instrument_key": document.instrument_key}) for chunk in chunks]


def republish_source_metadata(source: SourceRecord, chunks: list[Chunk]) -> list[Chunk]:
    """Re-derive the governed source metadata on already-published chunks.

    A provenance repair (official URL, dates, law status, citation role) has to
    reach retrieval without re-splitting a body that is not changing. This
    reproduces exactly the fields :func:`chunk_document` derives from a
    document, leaving text, chunk identity and chunk structure untouched.
    """

    role = citation_role_for_source(source)
    published: list[Chunk] = []
    for chunk in chunks:
        heading_path = chunk.heading_path
        if heading_path and heading_path[0] == chunk.title:
            heading_path = [source.title, *heading_path[1:]]
        citation_label = chunk.citation_label
        if citation_label and chunk.title and citation_label.startswith(chunk.title):
            citation_label = f"{source.title}{citation_label[len(chunk.title) :]}"
        published.append(
            chunk.model_copy(
                update={
                    "library_kind": source.library_kind,
                    "title": source.title,
                    "doc_type": source.doc_type,
                    "citation_role": role,
                    "can_cite_clause": can_cite_clause_chunk(source, chunk.article_no),
                    "authority": source.authority,
                    "law_status": source.law_status,
                    "publish_date": source.publish_date,
                    "effective_date": source.effective_date,
                    "valid_to": source.valid_to,
                    "instrument_key": source.instrument_key,
                    "source_url": source.source_url,
                    "applicable_region": source.applicable_region,
                    "issuing_body": source.issuing_body,
                    "owning_department": source.owning_department,
                    "internal_status": source.internal_status,
                    "legal_domain": source.legal_domain,
                    "applicable_subjects": source.applicable_subjects,
                    "case_no": source.case_no,
                    "court": source.court,
                    "trial_instance": source.trial_instance,
                    "contract_parties": source.contract_parties,
                    "clause_type": source.clause_type,
                    "topic_tags": source.topic_tags,
                    "heading_path": heading_path,
                    "citation_label": citation_label,
                }
            )
        )
    return published


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
