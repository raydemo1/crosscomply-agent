"""Optional post-fusion reranking for review retrieval."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass

from law_agent.config import RerankConfig, RerankMode, require_rerank_config
from law_agent.review.schemas import RetrievalHit, RetrievalQuery, ReviewFacts

MAX_RERANK_QUERY_CHARS = 1200
MAX_RERANK_DOCUMENT_CHARS = 1400


@dataclass(frozen=True)
class RerankScore:
    index: int
    score: float


@dataclass(frozen=True)
class RerankOutcome:
    hits: list[RetrievalHit]
    info: dict[str, object]


class Reranker:
    """Shared interface for rerank providers."""

    def rerank(
        self,
        *,
        query: str,
        documents: Sequence[str],
        top_n: int,
    ) -> list[RerankScore]:
        raise NotImplementedError


class OpenAICompatibleReranker(Reranker):
    """Reranker client for OpenAI-compatible ``/rerank`` endpoints."""

    def __init__(self, config: RerankConfig) -> None:
        self.config = config
        self._endpoint = f"{config.base_url}/rerank"

    def rerank(
        self,
        *,
        query: str,
        documents: Sequence[str],
        top_n: int,
    ) -> list[RerankScore]:
        if not documents:
            return []
        payload = {
            "model": self.config.model,
            "query": query,
            "documents": list(documents),
            "top_n": min(top_n, len(documents)),
            "return_documents": False,
        }
        data = self._post(payload)
        raw_results = data.get("results", [])
        scores: list[RerankScore] = []
        for item in raw_results:
            index = item.get("index")
            score = item.get("relevance_score", item.get("score"))
            if isinstance(index, int) and isinstance(score, (int, float)):
                scores.append(RerankScore(index=index, score=float(score)))
        return scores

    def _post(self, payload: dict[str, object]) -> dict[str, object]:
        if not self.config.api_key:
            raise RuntimeError("RERANK_API_KEY is not configured")
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
                retryable = exc.code == 429 or exc.code >= 500
                if not retryable or attempt == max_retries - 1:
                    raise RuntimeError(
                        f"rerank request failed with HTTP {exc.code}: {detail}"
                    ) from exc
                time.sleep(min(2**attempt, 8))
            except urllib.error.URLError as exc:
                if attempt == max_retries - 1:
                    raise RuntimeError(f"rerank request failed: {exc}") from exc
                time.sleep(min(2**attempt, 8))
        raise RuntimeError("rerank request exhausted retries")


def build_reranker(config: RerankConfig) -> Reranker:
    if config.mode == "embedding":
        return OpenAICompatibleReranker(config)
    raise RuntimeError(f"unsupported rerank mode: {config.mode!r}")


def rerank_hits(
    hits: Sequence[RetrievalHit],
    *,
    question: str,
    material_text: str,
    facts: ReviewFacts,
    queries: Sequence[RetrievalQuery],
    top_k: int,
    mode: RerankMode = "off",
    config: RerankConfig | None = None,
    reranker: Reranker | None = None,
) -> RerankOutcome:
    """Optionally rerank fused hits and return a traceable outcome."""

    original_hits = list(hits)
    if mode == "off" or top_k <= 0 or len(original_hits) <= 1:
        return RerankOutcome(
            hits=original_hits[:top_k],
            info={
                "mode": "off",
                "input_count": len(original_hits),
                "output_count": min(len(original_hits), max(top_k, 0)),
            },
        )

    rerank_config = config or require_rerank_config(mode=mode)
    window = min(len(original_hits), max(top_k, rerank_config.window))
    window_hits = original_hits[:window]
    if reranker is None:
        reranker = build_reranker(rerank_config)

    query = _build_rerank_query(
        question=question,
        material_text=material_text,
        facts=facts,
        queries=queries,
    )
    documents = [_build_rerank_document(hit) for hit in window_hits]
    scores = reranker.rerank(query=query, documents=documents, top_n=window)

    ordered_hits = _apply_rerank_scores(
        window_hits,
        scores,
        blend_weight=rerank_config.blend_weight,
    )
    ordered_hits.extend(original_hits[window:])
    selected = [
        hit.model_copy(update={"rank": rank}) for rank, hit in enumerate(ordered_hits[:top_k])
    ]
    info = {
        "mode": rerank_config.mode,
        "model": rerank_config.model,
        "window": window,
        "input_count": len(original_hits),
        "output_count": len(selected),
        "returned_scores": len(scores),
        "blend_weight": rerank_config.blend_weight,
        "top_chunk_ids": [hit.chunk_id for hit in selected[:5]],
    }
    return RerankOutcome(hits=selected, info=info)


def _apply_rerank_scores(
    hits: Sequence[RetrievalHit],
    scores: Sequence[RerankScore],
    *,
    blend_weight: float,
) -> list[RetrievalHit]:
    rerank_by_index: dict[int, float] = {}
    for result in scores:
        if result.index < 0 or result.index >= len(hits):
            continue
        rerank_by_index[result.index] = result.score

    original_scores = [hit.score for hit in hits]
    rerank_scores = [rerank_by_index.get(index, 0.0) for index, _hit in enumerate(hits)]
    original_norm = _minmax_normalize(original_scores)
    rerank_norm = _minmax_normalize(rerank_scores)

    scored_hits: list[tuple[float, int, RetrievalHit]] = []
    for index, hit in enumerate(hits):
        final_score = (
            blend_weight * rerank_norm[index] + (1.0 - blend_weight) * original_norm[index]
        )
        scored_hits.append(
            (
                final_score,
                index,
                hit.model_copy(update={"score": round(final_score, 6)}),
            )
        )

    scored_hits.sort(key=lambda item: (-item[0], item[1], item[2].chunk_id))
    return [hit for _score, _index, hit in scored_hits]


def _minmax_normalize(values: Sequence[float]) -> list[float]:
    if not values:
        return []
    minimum = min(values)
    maximum = max(values)
    if maximum == minimum:
        return [1.0 for _value in values]
    return [(value - minimum) / (maximum - minimum) for value in values]


def _build_rerank_query(
    *,
    question: str,
    material_text: str,
    facts: ReviewFacts,
    queries: Sequence[RetrievalQuery],
) -> str:
    facts_parts: list[str] = []
    if facts.cross_border_transfer is not None:
        facts_parts.append(f"跨境传输={facts.cross_border_transfer}")
    if facts.region:
        facts_parts.append(f"地区={facts.region}")
    if facts.industry:
        facts_parts.append(f"行业={facts.industry}")
    if facts.data_types:
        facts_parts.append(f"数据类型={','.join(facts.data_types[:6])}")

    query_texts = "；".join(query.text for query in queries[:6])
    parts = [
        f"用户问题：{question.strip()}",
        f"业务事实：{'；'.join(facts_parts) if facts_parts else '无明确结构化事实'}",
    ]
    if query_texts:
        parts.append(f"检索意图：{query_texts}")
    if material_text.strip():
        parts.append(f"材料摘要：{material_text.strip()[:500]}")
    return "\n".join(parts)[:MAX_RERANK_QUERY_CHARS]


def _build_rerank_document(hit: RetrievalHit) -> str:
    parts = [
        f"标题：{hit.title}",
        f"证据角色：{hit.citation_role}",
        f"查询类型：{hit.matched_query_type or 'unknown'}",
        f"正文：{hit.text}",
    ]
    return "\n".join(parts)[:MAX_RERANK_DOCUMENT_CHARS]
