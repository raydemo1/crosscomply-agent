"""Always-on unit tests for embedding providers and ``ServiceConfig``.

These tests do not require any running service or optional dependency.
Gated end-to-end tests against real Elasticsearch + pgvector have been
removed; the service path is now exercised via injected adapters in
``test_review_hybrid_retrieval.py``.
"""

from __future__ import annotations

from law_agent.config import load_service_config
from law_agent.llm.embeddings import EmbeddingsProvider


class FakeEmbeddings(EmbeddingsProvider):
    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def embed_query(self, query: str) -> list[float]:
        value = float(sum(query.encode("utf-8")) % 11 + 1)
        return [value] + [0.0] * (self.dimension - 1)

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_query(text) for text in texts]

# ---------------------------------------------------------------------------
# Always-on: embedding providers and service config (no services required)
# ---------------------------------------------------------------------------


def test_fake_embeddings_are_deterministic_and_dimensioned() -> None:
    embeddings = FakeEmbeddings(dimension=8)
    a = embeddings.embed_query("数据出境")
    b = embeddings.embed_query("数据出境")
    c = embeddings.embed_query("different text")

    assert len(a) == 8
    assert a == b  # deterministic for identical input
    assert a != c  # different text -> different vector


def test_fake_embeddings_batch() -> None:
    embeddings = FakeEmbeddings(dimension=4)
    vectors = embeddings.embed_texts(["one", "two", "three"])
    assert len(vectors) == 3
    assert all(len(v) == 4 for v in vectors)


def test_service_config_has_es_pg_and_embedding_sections() -> None:
    config = load_service_config()
    assert config.elasticsearch.url
    assert config.elasticsearch.index_name
    assert config.postgres.dsn
    assert config.postgres.table_name
    assert config.embedding.dimension > 0
    assert config.embedding.provider in ("openai_compatible", "sentence_transformers")
