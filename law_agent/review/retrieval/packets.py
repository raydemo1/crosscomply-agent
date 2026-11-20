"""Source-level evidence packets the Agent reads after a search.

Each packet keeps the representative chunk of one source with its supporting
and neighbouring chunks, all enriched with authoritative metadata and, for
citable clauses, the complete article text.
"""

from __future__ import annotations

from collections.abc import Sequence

from law_agent.review.citations import assemble_article_text
from law_agent.review.schemas import RetrievalHit, SourceEvidencePacket

DEFAULT_SUPPORTING_CHUNKS_PER_SOURCE = 2


def _dedupe_hits(hits: list[RetrievalHit]) -> list[RetrievalHit]:
    seen: set[str] = set()
    deduped: list[RetrievalHit] = []
    for hit in hits:
        if hit.chunk_id in seen:
            continue
        seen.add(hit.chunk_id)
        deduped.append(hit)
    return deduped


def flatten_source_evidence_packets(
    packets: list[SourceEvidencePacket],
) -> list[RetrievalHit]:
    """Return packet evidence as an ordered, de-duplicated chunk list."""

    hits: list[RetrievalHit] = []
    for packet in packets:
        hits.append(packet.representative_chunk)
        hits.extend(packet.supporting_chunks)
        hits.extend(packet.neighbor_chunks)
    return _dedupe_hits(hits)


def enrich_hit_for_citation(
    hit: RetrievalHit,
    chunks_by_id: dict[str, object],
) -> RetrievalHit:
    """Attach authoritative metadata and the complete cited article to a hit."""

    chunk = chunks_by_id.get(hit.chunk_id)
    if chunk is None:
        return hit

    updates: dict[str, object] = {
        "doc_type": getattr(chunk, "doc_type", hit.doc_type),
        "authority": getattr(chunk, "authority", hit.authority),
        "law_status": getattr(chunk, "law_status", hit.law_status),
        "publish_date": getattr(chunk, "publish_date", hit.publish_date),
        "effective_date": getattr(chunk, "effective_date", hit.effective_date),
        "issuing_body": getattr(chunk, "issuing_body", hit.issuing_body),
        "article_no": getattr(chunk, "article_no", hit.article_no),
        "citation_label": getattr(chunk, "citation_label", hit.citation_label),
        "heading_path": getattr(chunk, "heading_path", hit.heading_path),
    }
    article_no = getattr(chunk, "article_no", None) or hit.article_no
    if hit.can_cite_clause and article_no:
        article_text = assemble_article_text(
            chunks_by_id, source_id=hit.source_id, article_no=article_no,
        )
        if article_text:
            updates["full_article_text"] = article_text
    return hit.model_copy(update=updates)


def build_source_evidence_packets(
    *,
    representative_hits: list[RetrievalHit],
    candidate_hits: list[RetrievalHit],
    neighbor_hits: list[RetrievalHit],
    query_hits: Sequence[RetrievalHit] = (),
    supporting_per_source: int = DEFAULT_SUPPORTING_CHUNKS_PER_SOURCE,
    chunks_by_id: dict[str, object] | None = None,
) -> list[SourceEvidencePacket]:
    """Attach chunk-level context to each source-aware representative hit."""

    lookup = chunks_by_id or {}
    enriched_representatives = [
        enrich_hit_for_citation(hit, lookup) for hit in representative_hits
    ]
    enriched_candidates = [enrich_hit_for_citation(hit, lookup) for hit in candidate_hits]
    enriched_neighbors = [enrich_hit_for_citation(hit, lookup) for hit in neighbor_hits]

    candidates_by_source: dict[str, list[RetrievalHit]] = {}
    for hit in sorted(enriched_candidates, key=lambda h: (h.rank, -h.score, h.chunk_id)):
        candidates_by_source.setdefault(hit.source_id, []).append(hit)

    neighbors_by_source: dict[str, list[RetrievalHit]] = {}
    for hit in enriched_neighbors:
        neighbors_by_source.setdefault(hit.source_id, []).append(hit)

    anchors_by_source: dict[str, list[RetrievalHit]] = {}
    for hit in query_hits:
        anchors_by_source.setdefault(hit.source_id, []).append(enrich_hit_for_citation(hit, lookup))

    packets: list[SourceEvidencePacket] = []
    for representative in enriched_representatives:
        anchors = _dedupe_hits([
            hit for hit in anchors_by_source.get(representative.source_id, [])
            if hit.chunk_id != representative.chunk_id
        ])
        other_supporting = [
            hit
            for hit in candidates_by_source.get(representative.source_id, [])
            if hit.chunk_id != representative.chunk_id
        ]
        neighbors = _dedupe_hits([
            hit
            for hit in neighbors_by_source.get(representative.source_id, [])
            if hit.chunk_id != representative.chunk_id
        ])
        supporting_budget = max(supporting_per_source, len(anchors))
        supporting = _dedupe_hits([*anchors, *other_supporting])[:supporting_budget]
        supporting_ids = {hit.chunk_id for hit in supporting}
        neighbors = [hit for hit in neighbors if hit.chunk_id not in supporting_ids]
        packets.append(
            SourceEvidencePacket(
                source_id=representative.source_id,
                title=representative.title,
                representative_chunk=representative,
                supporting_chunks=_dedupe_hits(supporting),
                neighbor_chunks=_dedupe_hits(neighbors),
            )
        )
    return packets
