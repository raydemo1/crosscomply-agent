"""Lossless evidence projection for the Agent's model input."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any

SOURCE_FIELDS = (
    "doc_id", "title", "source_url", "source_has_articles", "doc_type",
    "authority", "law_status", "publish_date", "effective_date", "issuing_body",
)


def compact_evidence_context(state_payload: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy(state_payload)
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for hit in payload["evidence"]:
        groups[hit["source_id"]].append(hit)
    sources = {}
    for source_id, hits in groups.items():
        shared = {
            key: hits[0][key] for key in SOURCE_FIELDS
            if all(hit[key] == hits[0][key] for hit in hits)
        }
        sources[source_id] = shared
        for hit in hits:
            for key in shared:
                del hit[key]
            if hit["full_article_text"] in (None, hit["text"]):
                del hit["full_article_text"]
    payload["evidence_sources"] = sources
    return payload
