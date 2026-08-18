# ruff: noqa: S110
"""Real service backends for Elasticsearch + PostgreSQL/pgvector retrieval.

This module turns the dependency-free adapters in ``adapters.py`` into fully
wired production retrieval. It owns:

* Elasticsearch client creation, index mapping (with IK Chinese analysis),
  and bulk indexing of chunks.
* PostgreSQL/pgvector schema creation, vector upsert, and a search function
  compatible with ``PgVectorAdapter``.
* ``build_service_adapters`` which assembles both adapters from a
  ``ServiceConfig`` plus an embeddings provider, preserving the fail-fast
  semantics of ``require_service_adapters`` (no local fallback).

Heavy dependencies (``elasticsearch``, ``psycopg``, ``pgvector``) are imported
lazily so the base environment and the existing test suite keep working without
the ``[service]`` extra installed.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from law_agent.config import ServiceConfig
from law_agent.data.schemas import Chunk
from law_agent.llm.embeddings import EmbeddingsProvider
from law_agent.review.retrieval.adapters import (
    ElasticsearchKeywordAdapter,
    PgVectorAdapter,
    require_service_adapters,
)
from law_agent.review.retrieval.indexing import chunk_index_document

_EMBEDDING_BATCH_SIZE = 16
_PG_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_pg_identifier(value: str, *, field_name: str = "identifier") -> str:
    """Return a conservative PostgreSQL identifier or fail before SQL building."""

    if not _PG_IDENTIFIER_RE.fullmatch(value):
        raise RuntimeError(f"{field_name} must match {_PG_IDENTIFIER_RE.pattern}; got {value!r}")
    return value


# ---------------------------------------------------------------------------
# Elasticsearch
# ---------------------------------------------------------------------------


def create_elasticsearch_client(config: ServiceConfig) -> Any:
    """Create a real Elasticsearch client from ``ServiceConfig``."""

    try:
        from elasticsearch import Elasticsearch
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "elasticsearch is not installed; install with pip install 'lawagent[service]'"
        ) from exc

    es_cfg = config.elasticsearch
    return Elasticsearch(
        es_cfg.url,
        api_key=es_cfg.api_key,
        verify_certs=es_cfg.verify_certs,
        request_timeout=30,
    )


def _installed_plugins(client: Any) -> set[str]:
    """Return the set of installed Elasticsearch plugin names, best-effort."""

    try:
        plugins = client.cat.plugins(format="json")
    except Exception:  # noqa: BLE001 - plugins discovery is best-effort
        return set()
    names: set[str] = set()
    for row in plugins or []:
        component = row.get("component") or row.get("name") or ""
        if component:
            names.add(str(component))
    return names


def _es_mapping(*, analyzer: str, search_analyzer: str) -> dict[str, Any]:
    """Build an Elasticsearch index mapping aligned with the adapter fields.

    The keyword adapter runs a ``multi_match`` over ``title``, ``citation_label``,
    ``heading_path``, ``text``, ``topic_tags`` and ``applicable_subjects``; this
    mapping gives the text-like fields the chosen analyzer (IK for Chinese when
    available) and keeps structured metadata as keywords for exact filters.
    """

    analyzed = {"type": "text", "analyzer": analyzer, "search_analyzer": search_analyzer}
    keyword = {"type": "keyword"}
    return {
        "properties": {
            "index_id": keyword,
            "chunk_id": keyword,
            "doc_id": keyword,
            "source_id": keyword,
            "title": {**analyzed, "fields": {"raw": keyword}},
            "text": analyzed,
            "chunk_index": {"type": "integer"},
            "doc_type": keyword,
            "heading_path": {"type": "text", "analyzer": analyzer},
            "article_no": keyword,
            "paragraph_no": keyword,
            "item_no": keyword,
            "citation_label": {**analyzed, "fields": {"raw": keyword}},
            "citation_role": keyword,
            "can_cite_clause": {"type": "boolean"},
            "prev_chunk_id": keyword,
            "next_chunk_id": keyword,
            "authority": keyword,
            "law_status": keyword,
            "publish_date": {"type": "date", "ignore_malformed": True},
            "effective_date": {"type": "date", "ignore_malformed": True},
            "source_url": keyword,
            "applicable_region": keyword,
            "issuing_body": keyword,
            "legal_domain": keyword,
            "applicable_subjects": {
                "type": "text",
                "analyzer": analyzer,
                "fields": {"raw": keyword},
            },
            "topic_tags": {"type": "text", "analyzer": analyzer, "fields": {"raw": keyword}},
            "char_count": {"type": "integer"},
            "generation_id": keyword,
            "retrieval_enabled": {"type": "boolean"},
        }
    }


def _resolve_analyzers(client: Any) -> tuple[str, str]:
    """Pick the best available Chinese analyzer pair.

    Prefers IK (``ik_max_word``/``ik_smart``); falls back to ``smartcn`` then
    the built-in ``standard`` analyzer so the index can still be created when
    no Chinese plugin is installed.
    """

    plugins = _installed_plugins(client)
    if "analysis-ik" in plugins:
        return "ik_max_word", "ik_smart"
    if "analysis-smartcn" in plugins:
        return "smartcn", "smartcn"
    return "standard", "standard"


def ensure_elasticsearch_index(client: Any, index_name: str) -> dict[str, str]:
    """Create the target index with a Chinese-aware mapping if absent.

    Returns the analyzer pair actually used. Existing indices are left
    untouched (re-creating requires explicit deletion).
    """

    analyzer, search_analyzer = _resolve_analyzers(client)
    if client.indices.exists(index=index_name):
        return {"analyzer": analyzer, "search_analyzer": search_analyzer}

    mapping = _es_mapping(analyzer=analyzer, search_analyzer=search_analyzer)
    # If the chosen analyzer is unavailable (e.g. plugin mis-detected), retry
    # with the always-present standard analyzer so indexing never hard-fails.
    try:
        client.indices.create(index=index_name, mappings=mapping)
    except Exception:  # noqa: BLE001 - fall back to standard analyzer
        standard_mapping = _es_mapping(analyzer="standard", search_analyzer="standard")
        client.indices.create(index=index_name, mappings=standard_mapping)
        return {"analyzer": "standard", "search_analyzer": "standard"}
    return {"analyzer": analyzer, "search_analyzer": search_analyzer}


def _bulk_actions(
    chunks: Sequence[Chunk],
    *,
    index_name: str,
    generation_id: str | None = None,
    retrieval_enabled: bool = True,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for chunk in chunks:
        document = chunk_index_document(
            chunk,
            generation_id=generation_id,
            retrieval_enabled=retrieval_enabled,
        )
        actions.append(
            {
                "_index": index_name,
                "_id": document["index_id"],
                "_source": document,
            }
        )
    return actions


def bulk_index_chunks(
    client: Any,
    index_name: str,
    chunks: Sequence[Chunk],
    *,
    generation_id: str | None = None,
    retrieval_enabled: bool = True,
) -> int:
    """Bulk-index chunks into Elasticsearch. Returns the number of actions."""

    try:
        from elasticsearch import helpers
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "elasticsearch is not installed; install with pip install 'lawagent[service]'"
        ) from exc

    actions = _bulk_actions(
        chunks,
        index_name=index_name,
        generation_id=generation_id,
        retrieval_enabled=retrieval_enabled,
    )
    if not actions:
        return 0
    success, _errors = helpers.bulk(client, actions, refresh=True)
    return int(success)


# ---------------------------------------------------------------------------
# PostgreSQL / pgvector
# ---------------------------------------------------------------------------


def create_postgres_connection(config: ServiceConfig) -> Any:
    """Create a psycopg connection from ``ServiceConfig``."""

    try:
        import psycopg
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "psycopg is not installed; install with pip install 'lawagent[service]'"
        ) from exc

    return psycopg.connect(config.postgres.dsn, autocommit=False)


def ensure_pgvector_schema(conn: Any, table_name: str, dimension: int) -> None:
    """Create the pgvector extension and chunks table if absent.

    The table mirrors ``chunk_index_document`` plus an ``embedding`` vector
    column of the configured dimension. A cosine distance index supports the
    similarity search used by ``make_pgvector_search_fn``.
    """

    table_name = _validate_pg_identifier(table_name, field_name="PG_TABLE")
    index_name = _validate_pg_identifier(
        f"{table_name}_embedding_idx", field_name="PG_TABLE index name"
    )
    with conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                index_id text PRIMARY KEY,
                chunk_id text,
                doc_id text,
                source_id text,
                title text,
                text text,
                chunk_index integer,
                doc_type text,
                heading_path text[],
                article_no text,
                paragraph_no text,
                item_no text,
                citation_label text,
                citation_role text,
                can_cite_clause boolean,
                prev_chunk_id text,
                next_chunk_id text,
                authority text,
                law_status text,
                publish_date text,
                effective_date text,
                source_url text,
                applicable_region text,
                issuing_body text,
                legal_domain text[],
                applicable_subjects text[],
                topic_tags text[],
                char_count integer,
                generation_id text,
                retrieval_enabled boolean NOT NULL DEFAULT true,
                embedding vector({dimension})
            );
            """
        )
        # Existing deployments already have the original table. These are
        # additive, online-safe migrations for staged generations.
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS generation_id text;")
        cur.execute(
            f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS retrieval_enabled boolean NOT NULL DEFAULT true;"
        )
        cur.execute(f"ALTER TABLE {table_name} ADD COLUMN IF NOT EXISTS index_id text;")
        cur.execute(
            f"UPDATE {table_name} SET index_id = COALESCE(generation_id, 'legacy') || ':' || chunk_id "
            "WHERE index_id IS NULL;"
        )
        cur.execute(f"ALTER TABLE {table_name} ALTER COLUMN index_id SET NOT NULL;")
        cur.execute(
            "SELECT att.attname FROM pg_index idx "
            "JOIN pg_attribute att ON att.attrelid = idx.indrelid AND att.attnum = ANY(idx.indkey) "
            "WHERE idx.indrelid = %s::regclass AND idx.indisprimary;",
            (table_name,),
        )
        primary_columns = {row[0] for row in cur.fetchall()}
        if primary_columns != {"index_id"}:
            cur.execute(f"ALTER TABLE {table_name} DROP CONSTRAINT IF EXISTS {table_name}_pkey;")
            cur.execute(f"ALTER TABLE {table_name} ADD PRIMARY KEY (index_id);")
        # HNSW index on cosine distance; IF NOT EXISTS keeps this idempotent.
        cur.execute(
            f"""
            CREATE INDEX IF NOT EXISTS {index_name}
            ON {table_name} USING hnsw (embedding vector_cosine_ops);
            """
        )
    conn.commit()


def _vector_literal(vector: list[float]) -> str:
    """Serialize a vector to the pgvector string form ``[v1,v2,...]``."""

    return "[" + ",".join(f"{float(v):.8f}" for v in vector) + "]"


def _pgvector_upsert_sql(table_name: str) -> str:
    table_name = _validate_pg_identifier(table_name, field_name="PG_TABLE")
    return f"""
        INSERT INTO {table_name} (
            index_id, chunk_id, doc_id, source_id, title, text, chunk_index, doc_type,
            heading_path, article_no, paragraph_no, item_no, citation_label,
            citation_role, can_cite_clause, prev_chunk_id, next_chunk_id,
            authority, law_status, publish_date, effective_date, source_url,
            applicable_region, issuing_body, legal_domain, applicable_subjects,
            topic_tags, char_count, generation_id, retrieval_enabled, embedding
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::vector
        )
        ON CONFLICT (index_id) DO UPDATE SET
            chunk_id = EXCLUDED.chunk_id,
            doc_id = EXCLUDED.doc_id,
            source_id = EXCLUDED.source_id,
            title = EXCLUDED.title,
            text = EXCLUDED.text,
            chunk_index = EXCLUDED.chunk_index,
            doc_type = EXCLUDED.doc_type,
            heading_path = EXCLUDED.heading_path,
            article_no = EXCLUDED.article_no,
            paragraph_no = EXCLUDED.paragraph_no,
            item_no = EXCLUDED.item_no,
            citation_label = EXCLUDED.citation_label,
            citation_role = EXCLUDED.citation_role,
            can_cite_clause = EXCLUDED.can_cite_clause,
            prev_chunk_id = EXCLUDED.prev_chunk_id,
            next_chunk_id = EXCLUDED.next_chunk_id,
            authority = EXCLUDED.authority,
            law_status = EXCLUDED.law_status,
            publish_date = EXCLUDED.publish_date,
            effective_date = EXCLUDED.effective_date,
            source_url = EXCLUDED.source_url,
            applicable_region = EXCLUDED.applicable_region,
            issuing_body = EXCLUDED.issuing_body,
            legal_domain = EXCLUDED.legal_domain,
            applicable_subjects = EXCLUDED.applicable_subjects,
            topic_tags = EXCLUDED.topic_tags,
            char_count = EXCLUDED.char_count,
            generation_id = EXCLUDED.generation_id,
            retrieval_enabled = EXCLUDED.retrieval_enabled,
            embedding = EXCLUDED.embedding
    """


def upsert_pgvector_rows(
    conn: Any,
    table_name: str,
    rows: Sequence[dict[str, Any]],
) -> int:
    """Upsert chunk rows (each with an ``embedding`` list) into pgvector."""

    if not rows:
        return 0
    sql = _pgvector_upsert_sql(table_name)
    column_order = [
        "index_id",
        "chunk_id",
        "doc_id",
        "source_id",
        "title",
        "text",
        "chunk_index",
        "doc_type",
        "heading_path",
        "article_no",
        "paragraph_no",
        "item_no",
        "citation_label",
        "citation_role",
        "can_cite_clause",
        "prev_chunk_id",
        "next_chunk_id",
        "authority",
        "law_status",
        "publish_date",
        "effective_date",
        "source_url",
        "applicable_region",
        "issuing_body",
        "legal_domain",
        "applicable_subjects",
        "topic_tags",
        "char_count",
        "generation_id",
        "retrieval_enabled",
    ]
    with conn.cursor() as cur:
        for row in rows:
            values = [row.get(col) for col in column_order]
            embedding = row.get("embedding")
            if embedding is None:
                raise RuntimeError(
                    f"chunk {row.get('chunk_id')} has no embedding; embed chunks before upserting"
                )
            values.append(_vector_literal(embedding))
            cur.execute(sql, values)
    conn.commit()
    return len(rows)


def make_pgvector_search_fn(
    conn: Any,
    table_name: str,
) -> Callable[[list[float], int], list[dict[str, Any]]]:
    """Return a ``search_fn(vector, top_k)`` for ``PgVectorAdapter``.

    Uses cosine distance (``<=>``) and converts it to a similarity score in
    ``[0, 1]`` (``1 - distance``) so higher is better, consistent with the
    other retrievers' scores.
    """

    table_name = _validate_pg_identifier(table_name, field_name="PG_TABLE")

    def search_fn(vector: list[float], top_k: int) -> list[dict[str, Any]]:
        literal = _vector_literal(vector)
        sql = f"""
            SELECT chunk_id, doc_id, source_id, title, text, citation_role,
                   can_cite_clause, source_url,
                   1 - (embedding <=> %s::vector) AS score
            FROM {table_name}
            WHERE embedding IS NOT NULL AND retrieval_enabled = true
            ORDER BY embedding <=> %s::vector
            LIMIT %s
        """
        with conn.cursor() as cur:
            cur.execute(sql, (literal, literal, top_k))
            colnames = [desc[0] for desc in cur.description]
            rows = [dict(zip(colnames, row)) for row in cur.fetchall()]
        # psycopg returns arrays as Python lists; JSON-serializable already.
        return rows

    return search_fn


# ---------------------------------------------------------------------------
# Adapter assembly
# ---------------------------------------------------------------------------


@dataclass
class ServiceAdapters:
    """Both service adapters plus a close hook for owned connections."""

    keyword: ElasticsearchKeywordAdapter
    vector: PgVectorAdapter
    _close: Callable[[], None] | None = None

    def close(self) -> None:
        if self._close is not None:
            self._close()


def build_service_adapters(
    config: ServiceConfig,
    *,
    embeddings: EmbeddingsProvider | None = None,
) -> ServiceAdapters:
    """Assemble ES + pgvector adapters from config.

    Opens real Elasticsearch and PostgreSQL connections and wires them into
    the dependency-free adapters. ``require_service_adapters`` is invoked to
    enforce that both routes exist (no local fallback).
    """

    from law_agent.llm.embeddings import build_embeddings_provider

    if embeddings is None:
        embeddings = build_embeddings_provider(config.embedding)

    es_client = None
    conn = None
    try:
        es_client = create_elasticsearch_client(config)
        conn = create_postgres_connection(config)
        ensure_pgvector_schema(conn, config.postgres.table_name, config.embedding.dimension)

        keyword_adapter = ElasticsearchKeywordAdapter(
            client=es_client, index_name=config.elasticsearch.index_name
        )
        vector_adapter = PgVectorAdapter(
            search_fn=make_pgvector_search_fn(conn, config.postgres.table_name),
            embed_texts=embeddings.embed_texts,
        )

        # Fail-fast: both adapters must be present.
        require_service_adapters(keyword=keyword_adapter, vector=vector_adapter)
    except Exception:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if es_client is not None:
            try:
                es_client.close()
            except Exception:  # noqa: BLE001
                pass
        raise

    def _close() -> None:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            es_client.close()
        except Exception:  # noqa: BLE001
            pass

    return ServiceAdapters(keyword=keyword_adapter, vector=vector_adapter, _close=_close)


# ---------------------------------------------------------------------------
# Corpus indexing convenience
# ---------------------------------------------------------------------------


def _embed_chunk_texts(
    chunks: Sequence[Chunk],
    embeddings: EmbeddingsProvider,
) -> dict[str, list[float]]:
    """Embed each chunk's ``title + text`` in batches, keyed by chunk_id.

    Paces requests with a small delay between batches to stay within the
    embedding provider's rate limit (SiliconCloud free tier in particular).
    """

    import time

    vectors: dict[str, list[float]] = {}
    batch: list[tuple[str, str]] = []
    for batch_no, chunk in enumerate(chunks):
        text = f"{chunk.title}\n{chunk.text}" if chunk.title else chunk.text
        batch.append((chunk.chunk_id, text))
        if len(batch) >= _EMBEDDING_BATCH_SIZE:
            _flush_embedding_batch(batch, embeddings, vectors)
            batch = []
            # Pacing: ~0.3s between batches to avoid transient 400/429.
            time.sleep(0.3)
    if batch:
        _flush_embedding_batch(batch, embeddings, vectors)
    return vectors


def _flush_embedding_batch(
    batch: list[tuple[str, str]],
    embeddings: EmbeddingsProvider,
    sink: dict[str, list[float]],
) -> None:
    texts = [text for _chunk_id, text in batch]
    vectors = embeddings.embed_texts(texts)
    if len(vectors) != len(batch):
        raise RuntimeError(
            f"embedding provider returned {len(vectors)} vectors for {len(batch)} texts"
        )
    for (chunk_id, _text), vector in zip(batch, vectors, strict=True):
        sink[chunk_id] = vector


def index_corpus_to_services(
    config: ServiceConfig,
    chunks: Sequence[Chunk],
    *,
    embeddings: EmbeddingsProvider | None = None,
) -> dict[str, Any]:
    """Index chunks into both Elasticsearch and pgvector.

    Ensures the ES index and pgvector table exist, embeds chunk text, and
    bulk/upsert-imports the corpus. Returns a summary with counts and the
    analyzer pair used.
    """

    from law_agent.llm.embeddings import build_embeddings_provider

    if embeddings is None:
        embeddings = build_embeddings_provider(config.embedding)

    vectors = _embed_chunk_texts(chunks, embeddings)
    if len(vectors) != len(chunks):
        raise RuntimeError(f"embedded {len(vectors)} chunks for {len(chunks)} input chunks")
    for chunk in chunks:
        vector = vectors.get(chunk.chunk_id)
        if vector is None:
            raise RuntimeError(f"missing embedding for chunk {chunk.chunk_id}")
        if len(vector) != config.embedding.dimension:
            raise RuntimeError(
                f"chunk {chunk.chunk_id} embedding dimension {len(vector)} "
                f"does not match configured dimension {config.embedding.dimension}"
            )

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
        pg_count = upsert_pgvector_rows(conn, config.postgres.table_name, rows)
        es_count = bulk_index_chunks(es_client, config.elasticsearch.index_name, chunks)
    finally:
        if es_client is not None:
            try:
                es_client.close()
            except Exception:  # noqa: BLE001
                pass
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    return {
        "elasticsearch_index": config.elasticsearch.index_name,
        "elasticsearch_docs": es_count,
        "elasticsearch_analyzer": analyzers,
        "pgvector_table": config.postgres.table_name,
        "pgvector_rows": pg_count,
        "embedding_dimension": config.embedding.dimension,
    }


def healthcheck(config: ServiceConfig) -> dict[str, Any]:
    """Probe ES and pgvector reachability for the gated integration test."""

    result: dict[str, Any] = {
        "elasticsearch": False,
        "postgres": False,
        "elasticsearch_index": config.elasticsearch.index_name,
        "elasticsearch_docs": 0,
        "pgvector_table": config.postgres.table_name,
        "pgvector_rows": 0,
    }
    client = None
    try:
        client = create_elasticsearch_client(config)
        info = client.info()
        result["elasticsearch"] = bool(info.get("version", {}).get("number"))
        if client.indices.exists(index=config.elasticsearch.index_name):
            count = client.count(index=config.elasticsearch.index_name)
            result["elasticsearch_docs"] = int(count.get("count", 0))
    except Exception as exc:  # noqa: BLE001
        result["elasticsearch_error"] = str(exc)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
    conn = None
    try:
        conn = create_postgres_connection(config)
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            table_name = _validate_pg_identifier(config.postgres.table_name, field_name="PG_TABLE")
            cur.execute(
                "SELECT to_regclass(%s)",
                (table_name,),
            )
            exists = cur.fetchone()[0] is not None
            if exists:
                cur.execute(f"SELECT count(*) FROM {table_name}")
                result["pgvector_rows"] = int(cur.fetchone()[0])
        result["postgres"] = True
    except Exception as exc:  # noqa: BLE001
        result["postgres_error"] = str(exc)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
    return result


# Re-export for callers that import from this module.
__all__ = [
    "ServiceAdapters",
    "build_service_adapters",
    "bulk_index_chunks",
    "create_elasticsearch_client",
    "create_postgres_connection",
    "ensure_elasticsearch_index",
    "ensure_pgvector_schema",
    "healthcheck",
    "index_corpus_to_services",
    "make_pgvector_search_fn",
    "upsert_pgvector_rows",
]
