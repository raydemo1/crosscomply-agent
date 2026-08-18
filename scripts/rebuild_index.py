"""Rebuild ES + pgvector index from chunks.jsonl, with progress output."""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from law_agent.config import require_service_config
from law_agent.review.retrieval.corpus import load_corpus


def main() -> int:
    print("Loading config and corpus...", flush=True)
    config = require_service_config()
    chunks = load_corpus()
    print(f"  chunks: {len(chunks)}", flush=True)

    from law_agent.llm.embeddings import build_embeddings_provider
    from law_agent.review.retrieval.service_backends import (
        _embed_chunk_texts,
        bulk_index_chunks,
        create_elasticsearch_client,
        create_postgres_connection,
        ensure_elasticsearch_index,
        ensure_pgvector_schema,
        upsert_pgvector_rows,
    )

    print("Building embeddings provider...", flush=True)
    embeddings = build_embeddings_provider(config.embedding)

    print(f"Embedding {len(chunks)} chunks...", flush=True)
    t0 = time.time()
    vectors = _embed_chunk_texts(chunks, embeddings)
    elapsed = time.time() - t0
    print(f"  embedded {len(vectors)} chunks in {elapsed:.1f}s", flush=True)

    if len(vectors) != len(chunks):
        print(f"ERROR: embedded {len(vectors)} for {len(chunks)} chunks", flush=True)
        return 1

    print("Connecting to Elasticsearch...", flush=True)
    es_client = create_elasticsearch_client(config)
    analyzers = ensure_elasticsearch_index(es_client, config.elasticsearch.index_name)
    print(f"  ES index: {config.elasticsearch.index_name}, analyzers: {analyzers}", flush=True)

    print("Connecting to pgvector...", flush=True)
    conn = create_postgres_connection(config)
    ensure_pgvector_schema(conn, config.postgres.table_name, config.embedding.dimension)
    print(
        f"  PG table: {config.postgres.table_name}, dim: {config.embedding.dimension}", flush=True
    )

    print("Upserting pgvector rows...", flush=True)
    from law_agent.review.retrieval.indexing import chunk_index_document

    rows = [
        {**chunk_index_document(chunk), "embedding": vectors[chunk.chunk_id]} for chunk in chunks
    ]
    pg_count = upsert_pgvector_rows(conn, config.postgres.table_name, rows)
    print(f"  pgvector rows: {pg_count}", flush=True)

    print("Bulk indexing Elasticsearch...", flush=True)
    es_count = bulk_index_chunks(es_client, config.elasticsearch.index_name, chunks)
    print(f"  ES docs: {es_count}", flush=True)

    es_client.close()
    conn.close()

    print("\nDone!", flush=True)
    print(f"  pgvector: {pg_count} rows", flush=True)
    print(f"  elasticsearch: {es_count} docs", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
