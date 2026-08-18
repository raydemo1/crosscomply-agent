"""Service-backed retrieval adapter interfaces.

The concrete adapters are dependency-free wrappers around injectable clients.
This keeps tests and local development independent from running Elasticsearch
or PostgreSQL while preserving the production boundary.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol

from law_agent.review.schemas import RetrievalHit, RetrievalQueryType


class KeywordSearchAdapter(Protocol):
    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        query_type: RetrievalQueryType | None = None,
    ) -> list[RetrievalHit]: ...

    def search_many(
        self,
        queries: Sequence[tuple[str, RetrievalQueryType | None]],
        *,
        top_k: int = 10,
    ) -> list[list[RetrievalHit]]: ...


class VectorSearchAdapter(Protocol):
    def search_many(
        self,
        queries: Sequence[tuple[str, RetrievalQueryType | None]],
        *,
        top_k: int = 10,
    ) -> list[list[RetrievalHit]]: ...


class ElasticsearchKeywordAdapter:
    """Keyword search adapter over an injected Elasticsearch-compatible client."""

    def __init__(self, *, client: Any, index_name: str) -> None:
        self.client = client
        self.index_name = index_name

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        query_type: RetrievalQueryType | None = None,
    ) -> list[RetrievalHit]:
        body = {
            "size": top_k,
            "query": {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "fields": [
                                    "title^2",
                                    "citation_label^2",
                                    "heading_path",
                                    "text",
                                    "topic_tags",
                                    "applicable_subjects",
                                ],
                            }
                        }
                    ],
                    # Legacy records without this field remain searchable;
                    # staged generations explicitly set it to false.
                    "must_not": [{"term": {"retrieval_enabled": False}}],
                }
            },
        }
        response = self.client.search(index=self.index_name, body=body)
        raw_hits = response.get("hits", {}).get("hits", [])
        return [
            _hit_from_source(
                raw.get("_source", {}),
                raw.get("_score", 0.0),
                rank,
                "elasticsearch",
                query_type,
            )
            for rank, raw in enumerate(raw_hits[:top_k])
        ]

    def search_many(
        self,
        queries: Sequence[tuple[str, RetrievalQueryType | None]],
        *,
        top_k: int = 10,
    ) -> list[list[RetrievalHit]]:
        return [
            self.search(query, top_k=top_k, query_type=query_type) for query, query_type in queries
        ]


class PgVectorAdapter:
    """Vector search adapter over an injected pgvector-compatible search function."""

    def __init__(
        self,
        *,
        search_fn: Callable[[list[float], int], list[dict[str, Any]]],
        embed_texts: Callable[[Sequence[str]], list[list[float]]],
    ) -> None:
        self.search_fn = search_fn
        self.embed_texts = embed_texts
        self._query_cache: dict[str, list[float]] = {}

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        query_type: RetrievalQueryType | None = None,
    ) -> list[RetrievalHit]:
        vector = self._embed_one(query)
        rows = self.search_fn(vector, top_k)
        return [
            _hit_from_source(row, row.get("score", 0.0), rank, "pgvector", query_type)
            for rank, row in enumerate(rows[:top_k])
        ]

    def prewarm_queries(self, queries: Sequence[str]) -> int:
        """Embed uncached query strings in one batch. Returns newly cached count."""

        unique_missing: list[str] = []
        seen: set[str] = set()
        for query in queries:
            if query in self._query_cache or query in seen:
                continue
            seen.add(query)
            unique_missing.append(query)
        if not unique_missing:
            return 0

        vectors = self.embed_texts(unique_missing)
        if len(vectors) != len(unique_missing):
            raise RuntimeError(
                f"embedding provider returned {len(vectors)} vectors for "
                f"{len(unique_missing)} queries"
            )
        for query, vector in zip(unique_missing, vectors, strict=True):
            self._query_cache[query] = vector
        return len(unique_missing)

    def search_many(
        self,
        queries: Sequence[tuple[str, RetrievalQueryType | None]],
        *,
        top_k: int = 10,
    ) -> list[list[RetrievalHit]]:
        self.prewarm_queries([query for query, _query_type in queries])
        results: list[list[RetrievalHit]] = []
        for query, query_type in queries:
            rows = self.search_fn(self._query_cache[query], top_k)
            results.append(
                [
                    _hit_from_source(row, row.get("score", 0.0), rank, "pgvector", query_type)
                    for rank, row in enumerate(rows[:top_k])
                ]
            )
        return results

    def _embed_one(self, query: str) -> list[float]:
        cached = self._query_cache.get(query)
        if cached is not None:
            return cached
        self.prewarm_queries([query])
        vector = self._query_cache[query]
        return vector


def require_service_adapters(
    *,
    keyword: KeywordSearchAdapter | None,
    vector: VectorSearchAdapter | None,
) -> tuple[KeywordSearchAdapter, VectorSearchAdapter]:
    """Return both service adapters or fail without falling back."""

    if keyword is None and vector is None:
        raise RuntimeError("service retrieval requires Elasticsearch and pgvector adapters")
    if keyword is None:
        raise RuntimeError("service retrieval requires Elasticsearch adapter")
    if vector is None:
        raise RuntimeError("service retrieval requires pgvector adapter")
    return keyword, vector


def _hit_from_source(
    source: dict[str, Any],
    score: float,
    rank: int,
    retriever: str,
    query_type: RetrievalQueryType | None,
) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=str(source["chunk_id"]),
        doc_id=str(source["doc_id"]),
        source_id=str(source["source_id"]),
        title=str(source["title"]),
        text=str(source["text"]),
        score=float(score),
        rank=rank,
        retriever=retriever,
        citation_role=source.get("citation_role", "interpretation_auxiliary"),
        can_cite_clause=bool(source.get("can_cite_clause", False)),
        source_url=str(source.get("source_url", "")),
        matched_query_type=query_type,
        article_no=source.get("article_no"),
        citation_label=source.get("citation_label"),
        heading_path=list(source.get("heading_path") or []),
        doc_type=source.get("doc_type", "law"),
        authority=source.get("authority", "unknown"),
        law_status=source.get("law_status", "unknown"),
        publish_date=source.get("publish_date"),
        effective_date=source.get("effective_date"),
        issuing_body=source.get("issuing_body"),
    )
