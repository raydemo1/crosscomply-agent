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


def test_index_document_uses_generation_scoped_physical_id() -> None:
    from law_agent.review.retrieval.indexing import chunk_index_document

    document = chunk_index_document(
        FIXTURE_CHUNKS[0], generation_id="next", retrieval_enabled=False
    )

    assert document["chunk_id"] == FIXTURE_CHUNKS[0].chunk_id
    assert document["index_id"] == f"next:{FIXTURE_CHUNKS[0].chunk_id}"
    assert document["retrieval_enabled"] is False


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


# ---------------------------------------------------------------------------
# Resumable service indexing (checkpoint per batch)
# ---------------------------------------------------------------------------


class _CountingEmbeddings:
    """Records which texts were embedded; skips are proven by this log."""

    def __init__(self) -> None:
        self.batches: list[list[str]] = []

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [[0.1, 0.2, 0.3] for _text in texts]


def _patch_index_stores(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pg_ids: set[str],
    es_ids: set[str],
):
    import types

    from law_agent.review.retrieval import service_backends as backends

    written_pg: list[str] = []
    written_es: list[str] = []

    class FakeConn:
        def close(self) -> None:
            pass

    class FakeIndices:
        def refresh(self, *, index: str) -> None:
            pass

    class FakeES:
        def __init__(self) -> None:
            self.indices = FakeIndices()

        def count(self, *, index: str) -> dict:
            return {"count": len(es_ids)}

        def close(self) -> None:
            pass

    monkeypatch.setattr(backends, "create_elasticsearch_client", lambda _config: FakeES())
    monkeypatch.setattr(backends, "create_postgres_connection", lambda _config: FakeConn())
    monkeypatch.setattr(
        backends, "ensure_elasticsearch_index", lambda _client, _name: {"analyzer": "ik_max_word"}
    )
    monkeypatch.setattr(backends, "ensure_pgvector_schema", lambda _conn, _table, _dim: None)
    monkeypatch.setattr(backends, "_existing_pg_chunk_ids", lambda _conn, _table: set(pg_ids))
    monkeypatch.setattr(backends, "_existing_es_chunk_ids", lambda _client, _name: set(es_ids))

    def fake_upsert(_conn, _table, rows):
        written_pg.extend(row["chunk_id"] for row in rows)
        return len(rows)

    def fake_bulk(_client, _name, chunks, **_kwargs):
        written_es.extend(chunk.chunk_id for chunk in chunks)
        return len(chunks)

    monkeypatch.setattr(backends, "upsert_pgvector_rows", fake_upsert)
    monkeypatch.setattr(backends, "bulk_index_chunks", fake_bulk)

    config = types.SimpleNamespace(
        embedding=types.SimpleNamespace(dimension=3),
        elasticsearch=types.SimpleNamespace(index_name="lawagent_chunks"),
        postgres=types.SimpleNamespace(table_name="lawagent_chunks"),
    )
    return config, written_pg, written_es


def test_index_to_services_checkpoints_every_batch(monkeypatch: pytest.MonkeyPatch) -> None:
    from law_agent.review.retrieval import service_backends as backends

    config, written_pg, written_es = _patch_index_stores(
        monkeypatch, pg_ids=set(), es_ids=set()
    )
    embeddings = _CountingEmbeddings()
    progress: list[str] = []

    summary = backends.index_corpus_to_services(
        config,
        FIXTURE_CHUNKS,
        embeddings=embeddings,
        batch_size=2,
        progress=progress.append,
    )

    expected_ids = [chunk.chunk_id for chunk in FIXTURE_CHUNKS]
    assert written_pg == expected_ids
    assert written_es == expected_ids
    # 6 chunks / batch size 2 -> 3 embedding round trips, none skipped.
    assert [len(batch) for batch in embeddings.batches] == [2, 2, 2]
    assert summary["total_chunks"] == 6
    assert summary["already_indexed"] == 0
    assert summary["newly_indexed"] == 6
    assert progress[0].startswith("0/6 chunks already indexed")
    assert progress[-1].startswith("indexed 6/6 chunks")


def test_index_to_services_skips_chunks_in_both_stores(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from law_agent.review.retrieval import service_backends as backends

    done = {chunk.chunk_id for chunk in FIXTURE_CHUNKS[:4]}
    config, written_pg, written_es = _patch_index_stores(
        monkeypatch, pg_ids=set(done), es_ids=set(done)
    )
    embeddings = _CountingEmbeddings()

    summary = backends.index_corpus_to_services(
        config, FIXTURE_CHUNKS, embeddings=embeddings, batch_size=2
    )

    pending_ids = [chunk.chunk_id for chunk in FIXTURE_CHUNKS[4:]]
    assert written_pg == pending_ids
    assert written_es == pending_ids
    # Already-indexed chunks are never re-embedded; pending ones are.
    embedded_texts = [text for batch in embeddings.batches for text in batch]
    assert any("负面清单" in text for text in embedded_texts)
    assert any("答记者问" in text for text in embedded_texts)
    assert all("汽车数据处理者" not in text for text in embedded_texts)
    assert all("标准合同的方式" not in text for text in embedded_texts)
    assert summary["already_indexed"] == 4
    assert summary["newly_indexed"] == 2


def test_index_to_services_recovers_after_inter_store_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # pg has chunk_assessment + chunk_contract; ES only has chunk_contract.
    # chunk_assessment committed to pg but never reached ES -> it must be
    # treated as pending and rewritten idempotently on the next run.
    from law_agent.review.retrieval import service_backends as backends

    pg_ids = {"chunk_assessment", "chunk_contract"}
    es_ids = {"chunk_contract"}
    config, written_pg, written_es = _patch_index_stores(
        monkeypatch, pg_ids=pg_ids, es_ids=es_ids
    )
    embeddings = _CountingEmbeddings()

    summary = backends.index_corpus_to_services(
        config, FIXTURE_CHUNKS, embeddings=embeddings, batch_size=3
    )

    assert "chunk_assessment" in written_pg
    assert "chunk_assessment" in written_es
    assert "chunk_contract" not in written_pg
    assert "chunk_contract" not in written_es
    assert summary["already_indexed"] == 1
    assert summary["newly_indexed"] == 5
    embedded_ids_text = " ".join(
        text for batch in embeddings.batches for text in batch
    )
    assert "数据出境安全评估办法" in embedded_ids_text
