"""Service configuration checks without external services."""

from __future__ import annotations

from law_agent.config import load_service_config


def test_service_config_has_es_pg_and_embedding_sections() -> None:
    config = load_service_config()
    assert config.elasticsearch.url
    assert config.elasticsearch.index_name
    assert config.postgres.dsn
    assert config.postgres.table_name
    assert config.embedding.dimension > 0
    assert config.embedding.provider in ("openai_compatible", "sentence_transformers")
