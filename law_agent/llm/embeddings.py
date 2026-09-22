"""Pluggable embedding providers for pgvector indexing and query.

The retrieval adapters only depend on the ``embed_query`` / ``embed_texts``
callables, so the concrete embedding source stays swappable:

* ``OpenAICompatibleEmbeddings``  -- any ``/embeddings`` OpenAI-compatible host.
* ``LocalSentenceTransformerEmbeddings`` -- local model via ``sentence-transformers``.
* ``MockEmbeddings`` -- deterministic hash-based vectors for tests / dry runs.

DeepSeek (the project's chat provider) does not expose an embeddings endpoint,
which is why this module is intentionally separate from ``openai_compatible.py``
and configured via ``EmbeddingConfig``.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
import urllib.error
import urllib.request
from collections.abc import Sequence

from law_agent.config import EmbeddingConfig


class EmbeddingsProvider:
    """Shared interface for all embedding providers."""

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_query(self, query: str) -> list[float]:
        raise NotImplementedError


class OpenAICompatibleEmbeddings(EmbeddingsProvider):
    """Embeddings client for an OpenAI-compatible ``/embeddings`` endpoint."""

    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        self._endpoint = f"{config.base_url}/embeddings"

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = {"model": self.config.model, "input": list(texts)}
        data = self._post(payload)
        embeddings = data["data"]
        # API may return embeddings out of order; sort by index.
        embeddings.sort(key=lambda item: item.get("index", 0))
        return [item["embedding"] for item in embeddings]

    def embed_query(self, query: str) -> list[float]:
        return self.embed_texts([query])[0]

    def _post(self, payload: dict) -> dict:
        if not self.config.api_key:
            raise RuntimeError("EMBEDDING_API_KEY is not configured")
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

        max_retries = 4
        for attempt in range(max_retries):
            request = urllib.request.Request(
                self._endpoint,
                data=body,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.config.timeout_seconds
                ) as response:
                    return json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")
                # Retry on rate limit (429) and server errors (500+) only.
                # 400 is a real parameter error (e.g. oversized input) and
                # should surface immediately rather than waste retry budget.
                retryable = exc.code == 429 or exc.code >= 500
                if not retryable or attempt == max_retries - 1:
                    raise RuntimeError(
                        f"embedding request failed with HTTP {exc.code}: {detail}"
                    ) from exc
                backoff = min(2**attempt, 8)
                time.sleep(backoff)
            except (urllib.error.URLError, TimeoutError) as exc:
                # Python 3.12 surfaces socket read timeouts during
                # getresponse() as a bare TimeoutError instead of wrapping
                # it in URLError, so it must be retried explicitly.
                if attempt == max_retries - 1:
                    raise RuntimeError(f"embedding request failed: {exc}") from exc
                time.sleep(min(2**attempt, 8))
        raise RuntimeError("embedding request exhausted retries")


class LocalSentenceTransformerEmbeddings(EmbeddingsProvider):
    """Embeddings from a locally loaded sentence-transformers model.

    ``sentence-transformers`` is an optional dependency; it is imported lazily
    so the base environment stays lightweight.
    """

    def __init__(self, config: EmbeddingConfig) -> None:
        self.config = config
        self._model = None

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "sentence-transformers is not installed; install with "
                "pip install 'lawagent[local-embeddings]'"
            ) from exc
        self._model = SentenceTransformer(self.config.model)
        # Best-effort dim reconciliation with configured dimension.
        configured = self.config.dimension
        if configured and configured != self._dim():
            raise RuntimeError(
                f"configured EMBEDDING_DIM={configured} does not match "
                f"model '{self.config.model}' dimension {self._dim()}"
            )

    def _dim(self) -> int:
        assert self._model is not None
        return self._model.get_sentence_embedding_dimension()  # type: ignore[union-attr]

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        self._ensure_model()
        assert self._model is not None
        vectors = self._model.encode(list(texts)).tolist()  # type: ignore[union-attr]
        return [list(v) for v in vectors]

    def embed_query(self, query: str) -> list[float]:
        return self.embed_texts([query])[0]


class MockEmbeddings(EmbeddingsProvider):
    """Deterministic hash-based embeddings for tests and dry runs.

    Not semantically meaningful: identical text always yields the same vector,
    which is enough to exercise the pgvector pipeline end-to-end without an API
    key or a local model.
    """

    def __init__(self, dimension: int) -> None:
        self.dimension = dimension

    def _vector(self, text: str) -> list[float]:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        # Expand the 32-byte digest to fill ``dimension`` floats.
        raw = bytearray()
        while len(raw) < self.dimension:
            raw.extend(digest)
        values = [raw[i] for i in range(self.dimension)]
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        return [v / norm for v in values]

    def embed_texts(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, query: str) -> list[float]:
        return self._vector(query)


def build_embeddings_provider(config: EmbeddingConfig) -> EmbeddingsProvider:
    """Construct the embedding provider selected by ``EmbeddingConfig``."""

    if config.provider == "openai_compatible":
        return OpenAICompatibleEmbeddings(config)
    if config.provider == "sentence_transformers":
        return LocalSentenceTransformerEmbeddings(config)
    if config.provider == "mock":
        return MockEmbeddings(dimension=config.dimension)
    raise RuntimeError(f"unsupported embedding provider: {config.provider!r}")
