"""Optional post-fusion reranking for review retrieval."""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from law_agent.config import RerankConfig, RerankMode, require_rerank_config
from law_agent.data.schemas import Chunk
from law_agent.review.retrieval.fusion import DEFAULT_RRF_K
from law_agent.review.schemas import RetrievalHit, RetrievalQuery, ReviewFacts

MAX_RERANK_QUERY_CHARS = 1200
MAX_RERANK_DOCUMENT_CHARS = 1400

_DOC_TYPE_LABELS: dict[str, str] = {
    "law": "法律",
    "regulation": "法规规章",
    "policy": "政策文件",
    "faq": "官方问答",
    "guideline": "指引指南",
    "standard": "国家标准",
    "judicial_interpretation": "司法解释",
    "contract": "合同范本",
}

_CITATION_ROLE_LABELS: dict[str, str] = {
    "primary_legal_basis": "核心法定依据",
    "conditional_local_basis": "地方专项依据",
    "conditional_industry_basis": "行业专项依据",
    "implementation_reference": "实施参考",
    "interpretation_auxiliary": "官方解读",
}


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
            except OSError as exc:  # URLError, SSL EOF and timeouts on the response read
                if attempt == max_retries - 1:
                    raise RuntimeError(f"rerank request failed: {exc}") from exc
                time.sleep(min(2**attempt, 8))
        raise RuntimeError("rerank request exhausted retries")


def build_reranker(config: RerankConfig) -> Reranker:
    if config.mode == "cross_encoder":
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
    query_text: str | None = None,
    chunks_by_id: Mapping[str, Chunk] | None = None,
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

    query = query_text if query_text is not None else _build_rerank_query(
        question=question,
        material_text=material_text,
        facts=facts,
        queries=queries,
    )
    documents = [
        _build_rerank_document(hit, chunks_by_id.get(hit.chunk_id) if chunks_by_id else None)
        for hit in window_hits
    ]
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


_MAX_RRF_SCORE = 2.0 / DEFAULT_RRF_K


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

    original_norm = _normalize_original_scores([hit.score for hit in hits])
    rerank_cal = _calibrate_rerank_scores(rerank_by_index, len(hits))

    scored_hits: list[tuple[float, int, RetrievalHit]] = []
    for index, hit in enumerate(hits):
        final_score = (
            blend_weight * rerank_cal[index] + (1.0 - blend_weight) * original_norm[index]
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


def _calibrate_rerank_scores(rerank_by_index: Mapping[int, float], count: int) -> list[float]:
    """Preserve calibrated ``[0, 1]`` cross-encoder probabilities across queries.

    Standard ``/rerank`` endpoints (such as ``bge-reranker-v2-m3``) return
    sigmoid probabilities in ``[0, 1]``. Independent per-query min-max scaling
    would stretch a low-confidence query's top hit to ``1.0`` and break
    cross-query body allocation. If a provider returns unbounded logits outside
    ``[0, 1]``, apply a logistic sigmoid so scores remain comparable.
    """

    if count <= 0:
        return []
    returned = list(rerank_by_index.values())
    use_sigmoid = any(value < 0.0 or value > 1.0 for value in returned)
    calibrated: list[float] = []
    for index in range(count):
        if index not in rerank_by_index:
            calibrated.append(0.0)
            continue
        raw = rerank_by_index[index]
        if use_sigmoid:
            calibrated.append(1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, raw)))))
        else:
            calibrated.append(max(0.0, min(1.0, raw)))
    return calibrated


def _normalize_original_scores(values: Sequence[float]) -> list[float]:
    """Scale pre-rerank RRF scores against the fixed theoretical RRF maximum."""

    if not values:
        return []
    scale = max(_MAX_RRF_SCORE, max(values))
    if scale <= 0.0:
        return [0.0 for _value in values]
    return [max(0.0, min(1.0, value / scale)) for value in values]


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
    if facts.regions:
        facts_parts.append(f"地区={','.join(facts.regions)}")
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


def _build_rerank_document(hit: RetrievalHit, chunk: Chunk | None = None) -> str:
    title = f"{hit.title}（{hit.article_no}）" if hit.article_no else hit.title
    doc_type = _DOC_TYPE_LABELS.get(hit.doc_type, hit.doc_type)
    role = _CITATION_ROLE_LABELS.get(hit.citation_role, hit.citation_role)
    parts = [
        f"标题：{title}",
        f"类型：{doc_type}；证据角色：{role}",
    ]
    headings = [part for part in hit.heading_path if part and part != hit.title and part != hit.article_no]
    if headings:
        parts.append(f"章节：{' / '.join(headings[:4])}")
    if chunk is not None:
        scope_parts: list[str] = []
        region = (chunk.applicable_region or "").strip()
        if region:
            scope_parts.append("全国" if region == "CN" else region)
        if chunk.applicable_subjects:
            scope_parts.append("、".join(chunk.applicable_subjects[:4]))
        if scope_parts:
            parts.append(f"适用范围：{'；'.join(scope_parts)}")
    parts.append(f"正文：{hit.text}")
    return "\n".join(parts)[:MAX_RERANK_DOCUMENT_CHARS]
