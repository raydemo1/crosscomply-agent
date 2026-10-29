"""Select complete evidence by its strongest query match within a chunk limit."""

from __future__ import annotations

from collections.abc import Sequence

from law_agent.review.schemas import RetrievalHit


def select_evidence(candidate_hits: Sequence[RetrievalHit], *, max_chunks: int) -> list[RetrievalHit]:
    if type(max_chunks) is not int or max_chunks < 1:
        raise ValueError("max_chunks须为正整数")
    best: dict[str, RetrievalHit] = {}
    memberships: dict[str, list[str]] = {}
    for hit in candidate_hits:
        memberships.setdefault(hit.chunk_id, []).extend(hit.matched_query_ids)
        previous = best.get(hit.chunk_id)
        if previous is None or hit.score > previous.score:
            best[hit.chunk_id] = hit
    selected: list[RetrievalHit] = []
    units: set[tuple] = set()
    for hit in sorted(best.values(), key=lambda h: (-h.score, h.rank, h.chunk_id)):
        unit = ((hit.source_id, hit.doc_id, hit.article_no, hit.full_article_text)
                if hit.can_cite_clause and hit.article_no and hit.full_article_text else ("chunk", hit.chunk_id))
        if unit in units:
            continue
        selected.append(hit.model_copy(update={"matched_query_ids": list(dict.fromkeys(memberships[hit.chunk_id]))}))
        units.add(unit)
        if len(selected) == max_chunks:
            break
    return selected
