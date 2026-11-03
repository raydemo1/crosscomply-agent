"""Evidence projection preserving original text, identity and legal qualifications."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any

SOURCE_FIELDS = (
    "doc_id", "title", "source_url", "source_has_articles", "doc_type",
    "authority", "law_status", "publish_date", "effective_date", "issuing_body",
)
RETRIEVAL_FIELDS = frozenset({"score", "rank", "retriever", "matched_query_type", "matched_query_ids"})


def compact_evidence_context(state_payload: dict[str, Any]) -> dict[str, Any]:
    payload = deepcopy({key: value for key, value in state_payload.items() if key not in {"tool_session", "usage"}})
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for hit in payload["evidence"]:
        for key in RETRIEVAL_FIELDS:
            hit.pop(key, None)
        groups[hit["source_id"]].append(hit)
    sources = {}
    for source_id, hits in groups.items():
        shared = {
            key: hits[0][key] for key in SOURCE_FIELDS
            if all(hit[key] == hits[0][key] for hit in hits)
        }
        sources[source_id] = {**shared, "outline_read_request": {"source_id": source_id}}
        for hit in hits:
            for key in shared:
                del hit[key]
            if hit["full_article_text"] in (None, hit["text"]):
                del hit["full_article_text"]
            for key in list(hit):
                if hit[key] is None or hit[key] == []:
                    del hit[key]
        for key in list(sources[source_id]):
            if sources[source_id][key] is None or sources[source_id][key] == []:
                del sources[source_id][key]
    payload["evidence_sources"] = sources
    return payload


def search_observation_context(observation, previous_steps, evidence):
    if "candidate_navigation" not in observation:
        return observation
    seen = {}
    for step in previous_steps:
        if step.action == "search_evidence":
            for source in step.observation.get("candidate_navigation", []):
                for location in source.get("locations", []):
                    seen[(source["source_id"], location["chunk_id"])] = (source["title"], location)
    read = {hit.chunk_id for hit in evidence}
    read_articles = {(hit.source_id, hit.article_no) for hit in evidence if hit.full_article_text}
    candidates = []
    for source in observation.get("candidate_navigation", []):
        remaining = []
        for location in source["locations"]:
            if location["chunk_id"] in read or (source["source_id"], location.get("article_no")) in read_articles:
                continue
            previous = seen.get((source["source_id"], location["chunk_id"]))
            if previous == (source["title"], location):
                continue
            if previous and previous[0] == source["title"] and previous[1].get("preview") == location.get("preview"):
                location = {k: v for k, v in location.items() if k != "preview"}
            remaining.append(location)
        if remaining:
            candidates.append({**source, "locations": remaining})
    return {**observation, "candidate_navigation": candidates}
