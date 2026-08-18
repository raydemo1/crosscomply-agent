"""Service-backed indexing and retrieval for LegalBench-RAG-mini."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import replace
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import URLError

from law_agent.config import PostgresConfig, ServiceConfig, load_service_config
from law_agent.data.io import read_jsonl, write_json
from law_agent.data.schemas import Chunk
from law_agent.external.legalbench_rag.data import (
    DEFAULT_CHUNK_META_PATH,
    DEFAULT_CHUNKS_PATH,
    build_chunks,
    load_documents_for_queries,
    load_mini_queries,
    write_chunks_and_meta,
)
from law_agent.external.legalbench_rag.metrics import evaluate_case, mean
from law_agent.external.legalbench_rag.schemas import (
    LegalBenchChunkMeta,
    LegalBenchEvalSummary,
)
from law_agent.llm.embeddings import EmbeddingsProvider, build_embeddings_provider
from law_agent.review.retrieval.fusion import rrf_fuse, source_aware_fuse
from law_agent.review.retrieval.hits import merge_hits_by_chunk_id
from law_agent.review.retrieval.indexing import chunk_index_document
from law_agent.review.retrieval.service_backends import (
    build_service_adapters,
    bulk_index_chunks,
    create_elasticsearch_client,
    create_postgres_connection,
    ensure_elasticsearch_index,
    ensure_pgvector_schema,
    upsert_pgvector_rows,
)

DEFAULT_ES_INDEX = "lawagent_legalbench_mini"
DEFAULT_PG_TABLE = "legalbench_chunks"


def legalbench_service_config(
    *,
    es_index: str = DEFAULT_ES_INDEX,
    pg_table: str = DEFAULT_PG_TABLE,
) -> ServiceConfig:
    """Load service config and override only LegalBench-RAG index names."""

    base = load_service_config()
    return replace(
        base,
        elasticsearch=replace(base.elasticsearch, index_name=es_index),
        postgres=PostgresConfig(dsn=base.postgres.dsn, table_name=pg_table),
    )


def prepare_chunks(
    *,
    data_dir: Path,
    chunks_path: Path = DEFAULT_CHUNKS_PATH,
    chunk_meta_path: Path = DEFAULT_CHUNK_META_PATH,
    chunk_size: int = 1000,
    overlap: int = 200,
) -> dict[str, int | str]:
    queries = load_mini_queries(data_dir)
    documents = load_documents_for_queries(data_dir, queries)
    chunks, metas = build_chunks(documents, chunk_size=chunk_size, overlap=overlap)
    write_chunks_and_meta(chunks, metas, chunks_path=chunks_path, chunk_meta_path=chunk_meta_path)
    return {
        "documents": len(documents),
        "queries": len(queries),
        "chunks": len(chunks),
        "chunks_path": str(chunks_path),
        "chunk_meta_path": str(chunk_meta_path),
    }


def reset_legalbench_service_index(config: ServiceConfig) -> None:
    """Drop only the LegalBench-RAG ES index and pgvector table."""

    es_client = None
    conn = None
    try:
        es_client = create_elasticsearch_client(config)
        es_client.indices.delete(index=config.elasticsearch.index_name, ignore_unavailable=True)
        conn = create_postgres_connection(config)
        with conn.cursor() as cur:
            cur.execute(f"DROP TABLE IF EXISTS {config.postgres.table_name}")
        conn.commit()
    finally:
        if es_client is not None:
            es_client.close()
        if conn is not None:
            conn.close()


def index_legalbench(
    *,
    data_dir: Path,
    chunks_path: Path = DEFAULT_CHUNKS_PATH,
    chunk_meta_path: Path = DEFAULT_CHUNK_META_PATH,
    chunk_size: int = 1000,
    overlap: int = 200,
    es_index: str = DEFAULT_ES_INDEX,
    pg_table: str = DEFAULT_PG_TABLE,
    reset: bool = False,
    embedding_batch_size: int = 64,
    embedding_sleep_seconds: float = 0.0,
) -> dict[str, object]:
    """Prepare chunks and index them into the current service hybrid stack."""

    prep = prepare_chunks(
        data_dir=data_dir,
        chunks_path=chunks_path,
        chunk_meta_path=chunk_meta_path,
        chunk_size=chunk_size,
        overlap=overlap,
    )
    config = legalbench_service_config(es_index=es_index, pg_table=pg_table)
    if reset:
        reset_legalbench_service_index(config)
    chunks = read_jsonl(chunks_path, Chunk)
    summary = index_chunks_with_progress(
        config,
        chunks,
        embedding_batch_size=embedding_batch_size,
        embedding_sleep_seconds=embedding_sleep_seconds,
    )
    return {**prep, **summary}


def index_chunks_with_progress(
    config: ServiceConfig,
    chunks: Sequence[Chunk],
    *,
    embedding_batch_size: int,
    embedding_sleep_seconds: float,
    embeddings: EmbeddingsProvider | None = None,
) -> dict[str, object]:
    """Index chunks with larger configurable embedding batches and progress."""

    if embedding_batch_size < 1:
        raise ValueError("embedding_batch_size must be >= 1")
    if embeddings is None:
        embeddings = build_embeddings_provider(config.embedding)

    vectors = _embed_chunks_with_progress(
        chunks,
        embeddings,
        batch_size=embedding_batch_size,
        sleep_seconds=embedding_sleep_seconds,
        expected_dimension=config.embedding.dimension,
    )
    if len(vectors) != len(chunks):
        raise RuntimeError(f"embedded {len(vectors)} chunks for {len(chunks)} input chunks")

    es_client = None
    conn = None
    try:
        es_client = create_elasticsearch_client(config)
        analyzers = ensure_elasticsearch_index(es_client, config.elasticsearch.index_name)
        conn = create_postgres_connection(config)
        ensure_pgvector_schema(conn, config.postgres.table_name, config.embedding.dimension)
        rows = [
            {**chunk_index_document(chunk), "embedding": vectors[chunk.chunk_id]}
            for chunk in chunks
        ]
        print(f"upserting {len(rows)} pgvector rows", flush=True)
        pg_count = upsert_pgvector_rows(conn, config.postgres.table_name, rows)
        print(f"bulk indexing {len(chunks)} elasticsearch docs", flush=True)
        es_count = bulk_index_chunks(es_client, config.elasticsearch.index_name, chunks)
    finally:
        if es_client is not None:
            es_client.close()
        if conn is not None:
            conn.close()

    return {
        "elasticsearch_index": config.elasticsearch.index_name,
        "elasticsearch_docs": es_count,
        "elasticsearch_analyzer": analyzers,
        "pgvector_table": config.postgres.table_name,
        "pgvector_rows": pg_count,
        "embedding_dimension": config.embedding.dimension,
        "embedding_batch_size": embedding_batch_size,
    }


def _embed_chunks_with_progress(
    chunks: Sequence[Chunk],
    embeddings: EmbeddingsProvider,
    *,
    batch_size: int,
    sleep_seconds: float,
    expected_dimension: int,
) -> dict[str, list[float]]:
    vectors: dict[str, list[float]] = {}
    total = len(chunks)
    started = time.perf_counter()
    for start in range(0, total, batch_size):
        batch = chunks[start : start + batch_size]
        texts = [f"{chunk.title}\n{chunk.text}" for chunk in batch]
        batch_vectors = embeddings.embed_texts(texts)
        if len(batch_vectors) != len(batch):
            raise RuntimeError(
                f"embedding provider returned {len(batch_vectors)} vectors for {len(batch)} texts"
            )
        for chunk, vector in zip(batch, batch_vectors, strict=True):
            if len(vector) != expected_dimension:
                raise RuntimeError(
                    f"chunk {chunk.chunk_id} embedding dimension {len(vector)} "
                    f"does not match configured dimension {expected_dimension}"
                )
            vectors[chunk.chunk_id] = vector
        done = min(start + len(batch), total)
        elapsed = time.perf_counter() - started
        rate = done / elapsed if elapsed > 0 else 0.0
        print(
            f"embedded {done}/{total} chunks ({rate:.1f} chunks/s, batch={len(batch)})",
            flush=True,
        )
        if sleep_seconds > 0 and done < total:
            time.sleep(sleep_seconds)
    return vectors


def evaluate_legalbench(
    *,
    data_dir: Path,
    chunks_path: Path = DEFAULT_CHUNKS_PATH,
    chunk_meta_path: Path = DEFAULT_CHUNK_META_PATH,
    output_path: Path,
    report_path: Path | None = None,
    es_index: str = DEFAULT_ES_INDEX,
    pg_table: str = DEFAULT_PG_TABLE,
    top_k: int = 10,
    candidate_top_k: int = 50,
    query_embedding_batch_size: int = 64,
) -> LegalBenchEvalSummary:
    """Run retrieval-only eval using the current service hybrid retrieval shape."""

    queries = load_mini_queries(data_dir)
    chunks = read_jsonl(chunks_path, Chunk)
    metas = read_jsonl(chunk_meta_path, LegalBenchChunkMeta)
    chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    meta_by_id = {meta.chunk_id: meta for meta in metas}
    config = legalbench_service_config(es_index=es_index, pg_table=pg_table)
    adapters = build_service_adapters(config)
    case_results = []
    try:
        prewarm_vector_queries(
            adapters.vector,
            [query.query for query in queries],
            batch_size=query_embedding_batch_size,
        )
        for index, query in enumerate(queries, start=1):
            started = time.perf_counter()
            hits = retrieve_hybrid(
                query.query,
                adapters=adapters,
                chunks_by_id=chunks_by_id,
                top_k=top_k,
                candidate_top_k=candidate_top_k,
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            result = evaluate_case(query, hits, meta_by_id, latency_ms=latency_ms)
            case_results.append(result)
            if index % 25 == 0 or index == len(queries):
                print(f"evaluated {index}/{len(queries)} queries", flush=True)
    finally:
        adapters.close()

    summary = _build_summary(
        data_dir=data_dir,
        chunks_path=chunks_path,
        chunk_meta_path=chunk_meta_path,
        top_k=top_k,
        candidate_top_k=candidate_top_k,
        case_results=case_results,
    )
    write_json(output_path, summary.model_dump(mode="json"))
    if report_path is not None:
        write_report(summary, report_path)
    return summary


def prewarm_vector_queries(
    vector_adapter: object, queries: Sequence[str], *, batch_size: int
) -> None:
    """Pre-embed LegalBench queries in batches with transient-read retries."""

    if batch_size < 1:
        raise ValueError("query embedding batch size must be >= 1")
    unique_queries = list(dict.fromkeys(queries))
    cache = getattr(vector_adapter, "_query_cache", None)
    embed_texts = getattr(vector_adapter, "embed_texts", None)
    if not isinstance(cache, dict) or embed_texts is None:
        return
    total = len(unique_queries)
    for start in range(0, total, batch_size):
        batch = [
            query for query in unique_queries[start : start + batch_size] if query not in cache
        ]
        if not batch:
            continue
        vectors = _embed_texts_with_retries(embed_texts, batch)
        if len(vectors) != len(batch):
            raise RuntimeError(
                f"embedding provider returned {len(vectors)} vectors for {len(batch)} queries"
            )
        for query, vector in zip(batch, vectors, strict=True):
            cache[query] = vector
        print(
            f"prewarmed query embeddings {min(start + batch_size, total)}/{total}",
            flush=True,
        )


def _embed_texts_with_retries(embed_texts, texts: Sequence[str]) -> list[list[float]]:
    last_exc: Exception | None = None
    for attempt in range(4):
        try:
            return embed_texts(texts)
        except (IncompleteRead, URLError, TimeoutError) as exc:
            last_exc = exc
            time.sleep(min(2**attempt, 8))
    assert last_exc is not None
    raise RuntimeError(f"embedding request failed after retries: {last_exc}") from last_exc


def retrieve_hybrid(
    query: str,
    *,
    adapters: object,
    chunks_by_id: dict[str, Chunk],
    top_k: int,
    candidate_top_k: int,
):
    retrieval_queries = [(query, "legal_issue")]
    keyword_hits = merge_hits_by_chunk_id(
        adapters.keyword.search_many(retrieval_queries, top_k=candidate_top_k),
        top_k=candidate_top_k,
    )
    vector_hits = merge_hits_by_chunk_id(
        adapters.vector.search_many(retrieval_queries, top_k=candidate_top_k),
        top_k=candidate_top_k,
    )
    candidates = rrf_fuse(keyword_hits, vector_hits, top_k=candidate_top_k)
    return source_aware_fuse(candidates, top_k=top_k, chunks_by_id=chunks_by_id)


def write_report(summary: LegalBenchEvalSummary, path: Path) -> None:
    lines = [
        "# LegalBench-RAG-mini Retrieval Report",
        "",
        "Current LawAgent service hybrid retrieval only.",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Total cases | {summary.total_cases} |",
        f"| Doc Recall@5 | {summary.doc_recall_at_5:.4f} |",
        f"| Doc Recall@10 | {summary.doc_recall_at_10:.4f} |",
        f"| Span Recall@5 | {summary.span_recall_at_5:.4f} |",
        f"| Span Recall@10 | {summary.span_recall_at_10:.4f} |",
        f"| MRR@10 | {summary.mrr_at_10:.4f} |",
        f"| Mean best span overlap | {summary.mean_best_span_overlap:.4f} |",
        f"| Mean precision@10 | {summary.mean_precision_at_10:.4f} |",
        f"| Avg latency ms | {summary.avg_latency_ms:.1f} |",
        f"| Bad cases | {summary.bad_case_count} |",
        "",
        "## Top Bad Cases",
        "",
    ]
    for case in summary.bad_cases[:20]:
        lines.extend(
            [
                f"### {case.query_id}",
                "",
                f"- Tags: {', '.join(case.tags)}",
                f"- Query: {case.query}",
                f"- Expected files: {', '.join(case.expected_files)}",
                f"- Doc hit rank: {case.doc_hit_rank}",
                f"- Span hit rank: {case.span_hit_rank}",
                f"- Best span overlap: {case.best_span_overlap:.4f}",
                f"- Retrieved chunks: {', '.join(case.retrieved_chunk_ids[:5])}",
                "",
            ]
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def _build_summary(
    *,
    data_dir: Path,
    chunks_path: Path,
    chunk_meta_path: Path,
    top_k: int,
    candidate_top_k: int,
    case_results: list,
) -> LegalBenchEvalSummary:
    mrr_values = [
        1.0 / result.span_hit_rank
        if result.span_hit_rank is not None and result.span_hit_rank <= 10
        else 0.0
        for result in case_results
    ]
    bad_cases = [result for result in case_results if not result.span_recall_at_10]
    bad_cases.sort(key=lambda result: (result.best_span_overlap, result.doc_hit_rank or 999))
    return LegalBenchEvalSummary(
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        data_dir=str(data_dir),
        chunks_path=str(chunks_path),
        chunk_meta_path=str(chunk_meta_path),
        top_k=top_k,
        candidate_top_k=candidate_top_k,
        total_cases=len(case_results),
        doc_recall_at_5=mean([float(result.doc_recall_at_5) for result in case_results]),
        doc_recall_at_10=mean([float(result.doc_recall_at_10) for result in case_results]),
        span_recall_at_5=mean([float(result.span_recall_at_5) for result in case_results]),
        span_recall_at_10=mean([float(result.span_recall_at_10) for result in case_results]),
        mrr_at_10=mean(mrr_values),
        mean_best_span_overlap=mean([result.best_span_overlap for result in case_results]),
        mean_precision_at_10=mean([result.precision_at_10 for result in case_results]),
        avg_latency_ms=mean([float(result.latency_ms) for result in case_results]),
        bad_case_count=len(bad_cases),
        bad_cases=bad_cases[:50],
        all_case_results=case_results,
    )
