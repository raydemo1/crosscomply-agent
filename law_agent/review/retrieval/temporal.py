"""Choose legal versions valid at the case's review date."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

from law_agent.data.schemas import Chunk
from law_agent.review.schemas import RetrievalHit


def _parsed(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def filter_hits_as_of(
    hits: Sequence[RetrievalHit], chunks_by_id: Mapping[str, Chunk],
    *, as_of: date,
) -> list[RetrievalHit]:
    latest: dict[str, date] = {}
    for chunk in chunks_by_id.values():
        effective = _parsed(chunk.effective_date)
        if effective is None or effective > as_of:
            continue
        if _parsed(chunk.valid_to) and _parsed(chunk.valid_to) <= as_of:
            continue
        key = chunk.instrument_key or chunk.title.strip()
        if key:
            latest[key] = max(latest.get(key, date.min), effective)

    filtered: list[RetrievalHit] = []
    for hit in hits:
        chunk = chunks_by_id.get(hit.chunk_id)
        if chunk is None:
            continue
        effective = _parsed(chunk.effective_date)
        expires = _parsed(chunk.valid_to)
        if effective and effective > as_of:
            continue
        if expires and expires <= as_of:
            continue
        if chunk.law_status == "repealed" and expires is None:
            continue
        if chunk.law_status == "amended" and expires is None:
            continue
        key = chunk.instrument_key or chunk.title.strip()
        if effective and key and effective < latest.get(key, effective):
            continue
        filtered.append(
            hit.model_copy(update={"can_cite_clause": False})
            if chunk.law_status == "unknown" else hit
        )
    return filtered
