"""Semantic enrichment generation through a configured OpenAI-compatible LLM."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from law_agent.config import require_llm_config
from law_agent.data.schemas import (
    CleanedDocument,
    EnrichedDocument,
    Enrichment,
    EnrichmentMeta,
)
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient


def _llm_enrichment(document: CleanedDocument) -> Enrichment:
    config = require_llm_config()
    client = OpenAICompatibleClient(config)
    prompt = {
        "title": document.title,
        "doc_type": document.doc_type,
        "authority": document.authority,
        "law_status": document.law_status,
        "topic_tags": document.topic_tags,
        "text_excerpt": document.text[:6000],
        "required_schema": {
            "summary": "string",
            "keywords": ["string"],
            "questions": ["string"],
            "topic_tags": ["string"],
            "applicable_subjects": ["string"],
            "risk_tags": ["string"],
        },
    }
    data = client.chat_json(
        [
            ChatMessage(
                role="system",
                content=(
                    "你是法律合规 RAG 数据治理助手。只生成元数据，不改写法规正文。"
                    "必须输出 JSON object。"
                ),
            ),
            ChatMessage(role="user", content=json.dumps(prompt, ensure_ascii=False)),
        ]
    )
    return Enrichment(
        summary=str(data.get("summary", ""))[:500],
        keywords=list(data.get("keywords", [])),
        questions=list(data.get("questions", [])),
        topic_tags=list(data.get("topic_tags", document.topic_tags)),
        applicable_subjects=list(data.get("applicable_subjects", [])),
        authority_level=document.authority,
        risk_tags=list(data.get("risk_tags", [])),
        effective_status=document.law_status,
        enrichment_meta=EnrichmentMeta(
            model=config.model,
            prompt_version="0.1.0",
            generated_at=datetime.now(UTC).isoformat(),
        ),
    )


def enrich_document(document: CleanedDocument) -> EnrichedDocument:
    enrichment = _llm_enrichment(document)
    data = document.model_dump()
    data["enrichment"] = enrichment.model_dump()
    return EnrichedDocument.model_validate(data)
