"""Tests for service-backed retrieval adapters and indexing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from law_agent.data.io import write_jsonl
from law_agent.review.retrieval.adapters import (
    ElasticsearchKeywordAdapter,
    PgVectorAdapter,
    require_service_adapters,
)
from law_agent.review.retrieval.indexing import (
    build_elasticsearch_bulk_lines,
    build_pgvector_rows,
    write_elasticsearch_bulk_file,
    write_pgvector_rows_file,
)

from tests.test_review_retrieval_keyword import FIXTURE_CHUNKS


class FakeElasticsearchClient:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def search(self, *, index: str, body: dict) -> dict:
        self.calls.append({"index": index, "body": body})
        chunk = FIXTURE_CHUNKS[0]
        return {
            "hits": {
                "hits": [
                    {
                        "_score": 8.5,
                        "_source": {
                            "chunk_id": chunk.chunk_id,
                            "doc_id": chunk.doc_id,
                            "source_id": chunk.source_id,
                            "title": chunk.title,
                            "text": chunk.text,
                            "citation_role": chunk.citation_role,
                            "can_cite_clause": chunk.can_cite_clause,
                            "source_url": chunk.source_url,
                        },
                    }
                ]
            }
        }


def test_elasticsearch_adapter_maps_hits() -> None:
    client = FakeElasticsearchClient()
    adapter = ElasticsearchKeywordAdapter(client=client, index_name="lawagent_chunks")

    hits = adapter.search("数据出境", top_k=3, query_type="legal_issue")

    assert client.calls[0]["index"] == "lawagent_chunks"
    assert client.calls[0]["body"]["query"]["bool"]["must_not"] == [
        {"term": {"retrieval_enabled": False}}
    ]
    assert hits[0].retriever == "elasticsearch"
    assert hits[0].score == 8.5
    assert hits[0].matched_query_type == "legal_issue"


def test_pgvector_adapter_embeds_query_and_maps_rows() -> None:
    seen_vectors: list[list[float]] = []
    embed_batches: list[list[str]] = []
    chunk = FIXTURE_CHUNKS[0]

    def embed_texts(texts: list[str]) -> list[list[float]]:
        embed_batches.append(texts)
        return [[0.1, 0.2, 0.3] for _text in texts]

    def search_fn(vector: list[float], top_k: int) -> list[dict]:
        seen_vectors.append(vector)
        assert top_k == 2
        return [
            {
                "chunk_id": chunk.chunk_id,
                "doc_id": chunk.doc_id,
                "source_id": chunk.source_id,
                "title": chunk.title,
                "text": chunk.text,
                "score": 0.91,
                "citation_role": chunk.citation_role,
                "can_cite_clause": chunk.can_cite_clause,
                "source_url": chunk.source_url,
            }
        ]

    adapter = PgVectorAdapter(search_fn=search_fn, embed_texts=embed_texts)

    hits = adapter.search("数据出境", top_k=2, query_type="material_fact")

    assert embed_batches == [["数据出境"]]
    assert seen_vectors == [[0.1, 0.2, 0.3]]
    assert hits[0].retriever == "pgvector"
    assert hits[0].matched_query_type == "material_fact"


def test_pgvector_adapter_batches_and_caches_query_embeddings() -> None:
    embed_batches: list[list[str]] = []

    def embed_texts(texts: list[str]) -> list[list[float]]:
        embed_batches.append(texts)
        return [[float(idx)] for idx, _text in enumerate(texts)]

    def search_fn(vector: list[float], top_k: int) -> list[dict]:
        chunk = FIXTURE_CHUNKS[int(vector[0]) % len(FIXTURE_CHUNKS)]
        return [
            {
                "chunk_id": chunk.chunk_id,
                "doc_id": chunk.doc_id,
                "source_id": chunk.source_id,
                "title": chunk.title,
                "text": chunk.text,
                "score": 0.9,
                "citation_role": chunk.citation_role,
                "can_cite_clause": chunk.can_cite_clause,
                "source_url": chunk.source_url,
            }
        ]

    adapter = PgVectorAdapter(search_fn=search_fn, embed_texts=embed_texts)
    results = adapter.search_many(
        [("数据出境", "legal_issue"), ("数据出境", "material_fact"), ("标准合同", "legal_issue")],
        top_k=2,
    )
    adapter.search("标准合同", top_k=2)

    assert embed_batches == [["数据出境", "标准合同"]]
    assert len(results) == 3


def test_service_adapters_require_both_routes() -> None:
    keyword = ElasticsearchKeywordAdapter(
        client=FakeElasticsearchClient(),
        index_name="lawagent_chunks",
    )
    vector = PgVectorAdapter(search_fn=lambda vector, top_k: [], embed_texts=lambda texts: [])

    assert require_service_adapters(keyword=keyword, vector=vector) == (keyword, vector)
    with pytest.raises(RuntimeError, match="Elasticsearch"):
        require_service_adapters(keyword=None, vector=vector)
    with pytest.raises(RuntimeError, match="pgvector"):
        require_service_adapters(keyword=keyword, vector=None)
    with pytest.raises(RuntimeError, match="Elasticsearch and pgvector"):
        require_service_adapters(keyword=None, vector=None)


def test_build_elasticsearch_bulk_lines() -> None:
    lines = build_elasticsearch_bulk_lines(FIXTURE_CHUNKS[:1], index_name="lawagent")

    assert len(lines) == 2
    action = json.loads(lines[0])
    document = json.loads(lines[1])
    assert action == {"index": {"_index": "lawagent", "_id": FIXTURE_CHUNKS[0].chunk_id}}
    assert document["chunk_id"] == FIXTURE_CHUNKS[0].chunk_id
    assert document["citation_role"] == FIXTURE_CHUNKS[0].citation_role
    assert "text" in document


def test_build_pgvector_rows_includes_optional_embeddings() -> None:
    chunk = FIXTURE_CHUNKS[0]
    rows = build_pgvector_rows([chunk], embeddings={chunk.chunk_id: [0.1, 0.2]})

    assert rows[0]["chunk_id"] == chunk.chunk_id
    assert rows[0]["embedding"] == [0.1, 0.2]


def test_write_index_artifacts(tmp_path: Path) -> None:
    chunks_path = tmp_path / "chunks.jsonl"
    write_jsonl(chunks_path, FIXTURE_CHUNKS[:1])

    es_path = write_elasticsearch_bulk_file(
        chunks_path=chunks_path,
        output_path=tmp_path / "es_bulk.ndjson",
        index_name="lawagent",
    )
    pg_path = write_pgvector_rows_file(
        chunks_path=chunks_path,
        output_path=tmp_path / "pg_rows.jsonl",
    )

    assert es_path.read_text(encoding="utf-8").count("\n") == 2
    pg_row = json.loads(pg_path.read_text(encoding="utf-8").splitlines()[0])
    assert pg_row["chunk_id"] == FIXTURE_CHUNKS[0].chunk_id
    assert pg_row["embedding"] is None
