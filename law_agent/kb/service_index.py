"""Real ES + pgvector adapter for staged knowledge-base generations."""

from __future__ import annotations

from typing import Any

from law_agent.config import ServiceConfig
from law_agent.data.schemas import Chunk
from law_agent.kb.service import GenerationIndex
from law_agent.review.retrieval.indexing import chunk_index_document
from law_agent.review.retrieval.service_backends import (
    bulk_index_chunks,
    create_elasticsearch_client,
    create_postgres_connection,
    ensure_elasticsearch_index,
    ensure_pgvector_schema,
    upsert_pgvector_rows,
)


class ServiceGenerationIndex(GenerationIndex):
    """Stage a generation in both production retrieval stores.

    This adapter owns its connections for one CLI invocation.  Both stores are
    deliberately written with ``retrieval_enabled=false`` before the source
    pointer changes, so a failed staging write cannot surface partial evidence.
    """

    def __init__(self, config: ServiceConfig) -> None:
        self.config = config
        self.es = create_elasticsearch_client(config)
        self.pg = create_postgres_connection(config)
        ensure_elasticsearch_index(self.es, config.elasticsearch.index_name)
        ensure_pgvector_schema(self.pg, config.postgres.table_name, config.embedding.dimension)

    def close(self) -> None:
        self.pg.close()
        self.es.close()

    def stage(
        self,
        source_id: str,
        generation_id: str,
        chunks: list[Chunk],
        embeddings: dict[str, list[float]],
    ) -> None:
        for chunk in chunks:
            vector = embeddings[chunk.chunk_id]
            if len(vector) != self.config.embedding.dimension:
                raise RuntimeError(
                    f"chunk {chunk.chunk_id} embedding dimension {len(vector)} "
                    f"does not match configured dimension {self.config.embedding.dimension}"
                )
        rows = [
            {
                **chunk_index_document(
                    chunk, generation_id=generation_id, retrieval_enabled=False
                ),
                "embedding": embeddings[chunk.chunk_id],
            }
            for chunk in chunks
        ]
        upsert_pgvector_rows(self.pg, self.config.postgres.table_name, rows)
        bulk_index_chunks(
            self.es,
            self.config.elasticsearch.index_name,
            chunks,
            generation_id=generation_id,
            retrieval_enabled=False,
        )

    def verify(self, source_id: str, generation_id: str, expected_ids: set[str]) -> None:
        es_response = self.es.search(
            index=self.config.elasticsearch.index_name,
            body={
                "size": max(len(expected_ids), 1),
                "_source": ["chunk_id"],
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"source_id": source_id}},
                            {"term": {"generation_id": generation_id}},
                        ]
                    }
                },
            },
        )
        es_ids = {
            str(hit.get("_source", {}).get("chunk_id"))
            for hit in es_response.get("hits", {}).get("hits", [])
        }
        with self.pg.cursor() as cur:
            cur.execute(
                f"SELECT chunk_id FROM {self.config.postgres.table_name} "
                "WHERE source_id = %s AND generation_id = %s",
                (source_id, generation_id),
            )
            pg_ids = {str(row[0]) for row in cur.fetchall()}
        if es_ids != expected_ids or pg_ids != expected_ids:
            raise RuntimeError(
                "staged generation verification failed: Elasticsearch and pgvector "
                "must contain exactly the expected chunk IDs"
            )

    def activate(self, source_id: str, generation_id: str) -> str | None:
        with self.pg.cursor() as cur:
            cur.execute(
                f"SELECT DISTINCT generation_id FROM {self.config.postgres.table_name} "
                "WHERE source_id = %s AND retrieval_enabled = true",
                (source_id,),
            )
            active_rows = [row[0] for row in cur.fetchall()]
            generations = [generation for generation in active_rows if generation is not None]
            if len(generations) > 1:
                raise RuntimeError(f"source {source_id} has multiple active generations")
            # Legacy rows predate generation_id. Treat them as an explicit
            # cleanup target so the first source update removes them too.
            previous = generations[0] if generations else ("__legacy__" if active_rows else None)
            cur.execute(
                f"UPDATE {self.config.postgres.table_name} SET retrieval_enabled = false "
                "WHERE source_id = %s",
                (source_id,),
            )
            cur.execute(
                f"UPDATE {self.config.postgres.table_name} SET retrieval_enabled = true "
                "WHERE source_id = %s AND generation_id = %s",
                (source_id, generation_id),
            )
        self.pg.commit()
        self._set_es_enabled(source_id, generation_id)
        return previous

    def delete_generation(self, source_id: str, generation_id: str) -> None:
        generation_filter: dict[str, Any]
        pg_predicate: str
        pg_params: tuple[str, ...]
        if generation_id == "__legacy__":
            generation_filter = {"bool": {"must_not": [{"exists": {"field": "generation_id"}}]}}
            pg_predicate = "generation_id IS NULL"
            pg_params = (source_id,)
        else:
            generation_filter = {"term": {"generation_id": generation_id}}
            pg_predicate = "generation_id = %s"
            pg_params = (source_id, generation_id)
        self.es.delete_by_query(
            index=self.config.elasticsearch.index_name,
            refresh=True,
            conflicts="proceed",
            body={
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"source_id": source_id}},
                            generation_filter,
                        ]
                    }
                }
            },
        )
        with self.pg.cursor() as cur:
            cur.execute(
                f"DELETE FROM {self.config.postgres.table_name} "
                f"WHERE source_id = %s AND {pg_predicate}",
                pg_params,
            )
        self.pg.commit()

    def _set_es_enabled(self, source_id: str, generation_id: str) -> None:
        self.es.update_by_query(
            index=self.config.elasticsearch.index_name,
            refresh=True,
            conflicts="proceed",
            body={
                "script": {"source": "ctx._source.retrieval_enabled = false"},
                "query": {"term": {"source_id": source_id}},
            },
        )
        self.es.update_by_query(
            index=self.config.elasticsearch.index_name,
            refresh=True,
            conflicts="proceed",
            body={
                "script": {"source": "ctx._source.retrieval_enabled = true"},
                "query": {
                    "bool": {
                        "filter": [
                            {"term": {"source_id": source_id}},
                            {"term": {"generation_id": generation_id}},
                        ]
                    }
                },
            },
        )
