"""Metrics for LegalBench-RAG retrieval-only evaluation."""

from __future__ import annotations

from law_agent.external.legalbench_rag.schemas import (
    LegalBenchCaseResult,
    LegalBenchChunkMeta,
    LegalBenchQuery,
)
from law_agent.review.schemas import RetrievalHit


def evaluate_case(
    query: LegalBenchQuery,
    hits: list[RetrievalHit],
    chunk_meta_by_id: dict[str, LegalBenchChunkMeta],
    *,
    latency_ms: int,
) -> LegalBenchCaseResult:
    expected_files = sorted({snippet.file_path for snippet in query.snippets})
    retrieved = [hit for hit in hits if hit.chunk_id in chunk_meta_by_id]
    doc_hit_rank = _first_doc_hit_rank(query, retrieved, chunk_meta_by_id)
    span_hit_rank = _first_span_hit_rank(query, retrieved, chunk_meta_by_id)
    best_overlap = _best_span_overlap(query, retrieved, chunk_meta_by_id)
    precision = _precision_at_k(query, retrieved[:10], chunk_meta_by_id)
    return LegalBenchCaseResult(
        query_id=query.query_id,
        query=query.query,
        tags=query.tags,
        doc_hit_rank=doc_hit_rank,
        span_hit_rank=span_hit_rank,
        doc_recall_at_5=doc_hit_rank is not None and doc_hit_rank <= 5,
        doc_recall_at_10=doc_hit_rank is not None and doc_hit_rank <= 10,
        span_recall_at_5=span_hit_rank is not None and span_hit_rank <= 5,
        span_recall_at_10=span_hit_rank is not None and span_hit_rank <= 10,
        best_span_overlap=round(best_overlap, 6),
        precision_at_10=round(precision, 6),
        latency_ms=latency_ms,
        retrieved_chunk_ids=[hit.chunk_id for hit in retrieved[:10]],
        expected_files=expected_files,
    )


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _first_doc_hit_rank(
    query: LegalBenchQuery,
    hits: list[RetrievalHit],
    chunk_meta_by_id: dict[str, LegalBenchChunkMeta],
) -> int | None:
    expected_files = {snippet.file_path for snippet in query.snippets}
    for index, hit in enumerate(hits, start=1):
        meta = chunk_meta_by_id[hit.chunk_id]
        if meta.file_path in expected_files:
            return index
    return None


def _first_span_hit_rank(
    query: LegalBenchQuery,
    hits: list[RetrievalHit],
    chunk_meta_by_id: dict[str, LegalBenchChunkMeta],
) -> int | None:
    for index, hit in enumerate(hits, start=1):
        meta = chunk_meta_by_id[hit.chunk_id]
        if any(
            _span_overlap(meta, snippet.file_path, snippet.span) > 0 for snippet in query.snippets
        ):
            return index
    return None


def _best_span_overlap(
    query: LegalBenchQuery,
    hits: list[RetrievalHit],
    chunk_meta_by_id: dict[str, LegalBenchChunkMeta],
) -> float:
    best = 0.0
    for hit in hits:
        meta = chunk_meta_by_id[hit.chunk_id]
        for snippet in query.snippets:
            best = max(best, _span_overlap(meta, snippet.file_path, snippet.span))
    return best


def _precision_at_k(
    query: LegalBenchQuery,
    hits: list[RetrievalHit],
    chunk_meta_by_id: dict[str, LegalBenchChunkMeta],
) -> float:
    total_retrieved = 0
    relevant_retrieved = 0
    for hit in hits:
        meta = chunk_meta_by_id[hit.chunk_id]
        total_retrieved += max(0, meta.char_end - meta.char_start)
        for snippet in query.snippets:
            if meta.file_path != snippet.file_path:
                continue
            relevant_retrieved += _intersection_len((meta.char_start, meta.char_end), snippet.span)
    if total_retrieved == 0:
        return 0.0
    return relevant_retrieved / total_retrieved


def _span_overlap(
    meta: LegalBenchChunkMeta,
    expected_file_path: str,
    expected_span: tuple[int, int],
) -> float:
    if meta.file_path != expected_file_path:
        return 0.0
    expected_len = max(0, expected_span[1] - expected_span[0])
    if expected_len == 0:
        return 0.0
    return _intersection_len((meta.char_start, meta.char_end), expected_span) / expected_len


def _intersection_len(first: tuple[int, int], second: tuple[int, int]) -> int:
    return max(0, min(first[1], second[1]) - max(first[0], second[0]))
