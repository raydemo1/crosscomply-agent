"""Runtime configuration for LawAgent."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


def load_env_file(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE lines into the process environment."""

    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class LLMConfig:
    """OpenAI-compatible chat completion configuration."""

    base_url: str
    beta_base_url: str
    api_key: str | None
    model: str
    timeout_seconds: int
    structured_output_mode: Literal["json_object", "strict_tool"]
    reasoning_effort: Literal["none", "low", "medium", "high", "max"]

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


def load_llm_config() -> LLMConfig:
    """Load LLM settings from environment variables."""

    load_env_file()
    base_url = os.getenv("OPENAI_COMPATIBLE_BASE_URL", "https://api.deepseek.com").rstrip("/")
    timeout = os.getenv("OPENAI_COMPATIBLE_TIMEOUT_SECONDS", "60")
    structured_output_mode = os.getenv("OPENAI_COMPATIBLE_STRUCTURED_OUTPUT", "strict_tool")
    if structured_output_mode not in ("json_object", "strict_tool"):
        raise RuntimeError("OPENAI_COMPATIBLE_STRUCTURED_OUTPUT must be json_object or strict_tool")
    reasoning_effort = os.getenv("OPENAI_COMPATIBLE_REASONING_EFFORT", "none")
    if reasoning_effort not in ("none", "low", "medium", "high", "max"):
        raise RuntimeError(
            "OPENAI_COMPATIBLE_REASONING_EFFORT must be none, low, medium, high, or max"
        )
    return LLMConfig(
        base_url=base_url,
        beta_base_url=os.getenv("OPENAI_COMPATIBLE_BETA_BASE_URL", f"{base_url}/beta").rstrip("/"),
        api_key=os.getenv("OPENAI_COMPATIBLE_API_KEY") or None,
        model=os.getenv("OPENAI_COMPATIBLE_MODEL", "deepseek-v4-flash"),
        timeout_seconds=int(timeout),
        structured_output_mode=structured_output_mode,  # type: ignore[arg-type]
        reasoning_effort=reasoning_effort,  # type: ignore[arg-type]
    )


def require_llm_config() -> LLMConfig:
    """Load LLM settings and fail if mandatory values are missing."""

    config = load_llm_config()
    if not config.base_url:
        raise RuntimeError("OPENAI_COMPATIBLE_BASE_URL is required")
    if config.structured_output_mode == "strict_tool" and not config.beta_base_url:
        raise RuntimeError("OPENAI_COMPATIBLE_BETA_BASE_URL is required for strict_tool")
    if not config.api_key or config.api_key == "sk-your-deepseek-api-key":
        raise RuntimeError("OPENAI_COMPATIBLE_API_KEY is required")
    if not config.model:
        raise RuntimeError("OPENAI_COMPATIBLE_MODEL is required")
    return config


# ---------------------------------------------------------------------------
# Service retrieval configuration (Elasticsearch + pgvector + embeddings)
# ---------------------------------------------------------------------------

EmbeddingProvider = Literal["openai_compatible", "sentence_transformers", "mock"]
RerankMode = Literal["off", "embedding"]
DEFAULT_RERANK_WINDOW = 50


@dataclass(frozen=True)
class EmbeddingConfig:
    """Configuration for the embedding provider used by pgvector indexing.

    Kept separate from ``LLMConfig`` because the chat provider (DeepSeek) does
    not expose an embeddings endpoint; embeddings may come from a different
    OpenAI-compatible host, a local sentence-transformers model, or a
    deterministic mock for tests.
    """

    provider: EmbeddingProvider
    base_url: str
    api_key: str | None
    model: str
    dimension: int
    timeout_seconds: int


@dataclass(frozen=True)
class RerankConfig:
    """Configuration for optional post-fusion reranking."""

    mode: RerankMode
    base_url: str
    api_key: str | None
    model: str
    timeout_seconds: int
    window: int
    blend_weight: float


@dataclass(frozen=True)
class ElasticsearchConfig:
    url: str
    index_name: str
    api_key: str | None
    verify_certs: bool


@dataclass(frozen=True)
class PostgresConfig:
    dsn: str
    table_name: str


@dataclass(frozen=True)
class ServiceConfig:
    """Full service retrieval configuration (ES + pgvector + embeddings)."""

    elasticsearch: ElasticsearchConfig
    postgres: PostgresConfig
    embedding: EmbeddingConfig

    @property
    def enabled(self) -> bool:
        return bool(self.elasticsearch.url and self.postgres.dsn)


def _load_embedding_config() -> EmbeddingConfig:
    load_env_file()
    provider = os.getenv("EMBEDDING_PROVIDER", "openai_compatible")
    if provider not in ("openai_compatible", "sentence_transformers", "mock"):
        raise RuntimeError(
            f"EMBEDDING_PROVIDER={provider!r} is not supported; "
            "use openai_compatible, sentence_transformers, or mock"
        )
    return EmbeddingConfig(
        provider=provider,
        base_url=os.getenv("EMBEDDING_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        api_key=os.getenv("EMBEDDING_API_KEY") or None,
        model=os.getenv("EMBEDDING_MODEL", "text-embedding-3-small"),
        dimension=int(os.getenv("EMBEDDING_DIM", "1536")),
        timeout_seconds=int(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "60")),
    )


def _default_rerank_model(embedding_model: str) -> str:
    """Infer the paired reranker for common embedding models."""

    if embedding_model == "BAAI/bge-m3":
        return "BAAI/bge-reranker-v2-m3"
    if embedding_model == "Pro/BAAI/bge-m3":
        return "Pro/BAAI/bge-reranker-v2-m3"
    return embedding_model


def load_rerank_config(*, mode: RerankMode | None = None) -> RerankConfig:
    """Load optional rerank settings.

    Rerank is off by default. When enabled, it uses the same provider family as
    embeddings by default, with ``RERANK_*`` variables allowing an explicit
    override.
    """

    selected_mode = mode or os.getenv("RERANK_MODE", "off")
    if selected_mode not in ("off", "embedding"):
        raise RuntimeError("RERANK_MODE must be off or embedding")
    if selected_mode == "off":
        return RerankConfig(
            mode="off",
            base_url=os.getenv("RERANK_BASE_URL", "").rstrip("/"),
            api_key=os.getenv("RERANK_API_KEY") or None,
            model=os.getenv("RERANK_MODEL", ""),
            timeout_seconds=int(os.getenv("RERANK_TIMEOUT_SECONDS", "60")),
            window=int(os.getenv("RERANK_WINDOW", str(DEFAULT_RERANK_WINDOW))),
            blend_weight=float(os.getenv("RERANK_BLEND_WEIGHT", "0.4")),
        )

    embedding = _load_embedding_config()
    return RerankConfig(
        mode=selected_mode,  # type: ignore[arg-type]
        base_url=os.getenv("RERANK_BASE_URL", embedding.base_url).rstrip("/"),
        api_key=os.getenv("RERANK_API_KEY") or embedding.api_key,
        model=os.getenv(
            "RERANK_MODEL",
            os.getenv(
                "EMBEDDING_RERANK_MODEL",
                _default_rerank_model(embedding.model),
            ),
        ),
        timeout_seconds=int(os.getenv("RERANK_TIMEOUT_SECONDS", str(embedding.timeout_seconds))),
        window=int(os.getenv("RERANK_WINDOW", str(DEFAULT_RERANK_WINDOW))),
        blend_weight=float(os.getenv("RERANK_BLEND_WEIGHT", "0.4")),
    )


def require_rerank_config(*, mode: RerankMode | None = None) -> RerankConfig:
    """Load rerank settings and fail when enabled but incomplete."""

    config = load_rerank_config(mode=mode)
    if config.mode == "off":
        return config
    if not config.base_url:
        raise RuntimeError("RERANK_BASE_URL is required when rerank is enabled")
    if not config.api_key:
        raise RuntimeError("RERANK_API_KEY or EMBEDDING_API_KEY is required when rerank is enabled")
    if not config.model:
        raise RuntimeError("RERANK_MODEL is required when rerank is enabled")
    if config.window < 1:
        raise RuntimeError("RERANK_WINDOW must be >= 1")
    if not 0.0 <= config.blend_weight <= 1.0:
        raise RuntimeError("RERANK_BLEND_WEIGHT must be between 0 and 1")
    return config


def load_service_config() -> ServiceConfig:
    """Load service retrieval settings from environment variables."""

    load_env_file()
    es = ElasticsearchConfig(
        url=os.getenv("ES_URL", "http://localhost:9200").rstrip("/"),
        index_name=os.getenv("ES_INDEX", os.getenv("ES_INDEX_NAME", "lawagent_chunks")),
        api_key=os.getenv("ES_API_KEY") or None,
        verify_certs=os.getenv("ES_VERIFY_CERTS", "true").lower() == "true",
    )
    pg = PostgresConfig(
        dsn=os.getenv("PG_DSN", "postgresql://lawagent:lawagent@localhost:5432/lawagent"),
        table_name=os.getenv("PG_TABLE", "lawagent_chunks"),
    )
    return ServiceConfig(
        elasticsearch=es,
        postgres=pg,
        embedding=_load_embedding_config(),
    )


def require_service_config() -> ServiceConfig:
    """Load service settings and fail if mandatory values are missing.

    Mirrors the fail-fast semantics of ``require_service_adapters``: service
    retrieval must not silently fall back to local retrieval.
    """

    config = load_service_config()
    if not config.elasticsearch.url:
        raise RuntimeError("ES_URL is required for service retrieval")
    if not config.postgres.dsn:
        raise RuntimeError("PG_DSN is required for service retrieval")
    if config.embedding.provider == "openai_compatible" and not config.embedding.api_key:
        raise RuntimeError(
            "EMBEDDING_API_KEY is required when EMBEDDING_PROVIDER=openai_compatible"
        )
    if config.embedding.provider == "sentence_transformers" and not config.embedding.model:
        raise RuntimeError("EMBEDDING_MODEL is required for sentence_transformers provider")
    return config
