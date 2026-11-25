"""Share a batch's body slots across its queries, then fill by strongest match.

Fused scores from different queries are not comparable: a broad query scores
every generic clause high and would take all slots from a narrow one. Each
query therefore first gets its own best ``per_query_floor`` locations, in
round-robin order; the remaining slots go to the highest scores overall.
"""

from __future__ import annotations

from collections.abc import Sequence

from law_agent.review.schemas import RetrievalHit

DEFAULT_PER_QUERY_FLOOR = 2


def select_evidence(
    candidate_hits: Sequence[RetrievalHit],
    *,
    max_chunks: int,
    per_query_floor: int = DEFAULT_PER_QUERY_FLOOR,
    listed_depth: int | None = None,
) -> list[RetrievalHit]:
    if type(max_chunks) is not int or max_chunks < 1:
        raise ValueError("max_chunks须为正整数")
    best: dict[str, RetrievalHit] = {}
    best_rank: dict[str, int] = {}
    memberships: dict[str, list[str]] = {}
    per_query: dict[str, list[RetrievalHit]] = {}
    for hit in candidate_hits:
        memberships.setdefault(hit.chunk_id, []).extend(hit.matched_query_ids)
        for query_id in hit.matched_query_ids:
            per_query.setdefault(query_id, []).append(hit)
        previous = best.get(hit.chunk_id)
        if previous is None:
            best[hit.chunk_id] = hit
        elif listed_depth is not None:
            hit_listed = hit.rank < listed_depth
            prev_listed = previous.rank < listed_depth
            if hit_listed > prev_listed or (hit_listed == prev_listed and hit.score > previous.score):
                best[hit.chunk_id] = hit
        elif hit.score > previous.score:
            best[hit.chunk_id] = hit
        if hit.chunk_id not in best_rank or hit.rank < best_rank[hit.chunk_id]:
            best_rank[hit.chunk_id] = hit.rank

    selected: list[RetrievalHit] = []
    units: set[tuple] = set()

    def take(hit: RetrievalHit) -> bool:
        unit = _unit(hit)
        if unit in units:
            return False
        chosen = best[hit.chunk_id]
        selected.append(chosen.model_copy(update={"matched_query_ids": list(dict.fromkeys(memberships[hit.chunk_id]))}))
        units.add(unit)
        return True

    # Respect each query's post-spread rank order first so deferred items do not
    # reclaim the per-query floor slots over promoted legal clauses.
    queues = [sorted(hits, key=lambda h: (h.rank, -h.score, h.chunk_id)) for hits in per_query.values()]
    if len(queues) > 1:
        for _ in range(min(per_query_floor, max_chunks // len(queues))):
            for queue in queues:
                while queue and len(selected) < max_chunks and not take(queue.pop(0)):
                    pass
    for hit in sorted(
        best.values(),
        key=lambda h: (
            0 if listed_depth is None or best_rank[h.chunk_id] < listed_depth else 1,
            -h.score,
            best_rank[h.chunk_id],
            h.chunk_id,
        ),
    ):
        if len(selected) >= max_chunks:
            break
        take(hit)
    return selected


def _unit(hit: RetrievalHit) -> tuple:
    """The location one body slot stands for: a whole article, else a heading section.

    Splitting one article or section into several chunks must not let it fill
    several slots; the Agent reads the rest of a section through navigation.
    """

    if hit.can_cite_clause and hit.article_no and hit.full_article_text:
        return ("article", hit.source_id, hit.doc_id, hit.article_no, hit.full_article_text)
    if hit.article_no:
        return ("article", hit.source_id, hit.doc_id, hit.article_no)
    if hit.heading_path:
        return ("section", hit.source_id, hit.doc_id, *hit.heading_path)
    return ("chunk", hit.chunk_id)
