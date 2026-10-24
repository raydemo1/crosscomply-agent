"""Build candidate source manifests for data compliance topics."""

from __future__ import annotations

from law_agent.data.fetchers.flk_npc import search_sources
from law_agent.data.schemas import SourceRecord




def build_manifest(
    topic: str,
    *,
    from_flk: bool = False,
    terms: list[str] | None = None,
    limit: int | None = None,
    timeout_seconds: int = 30,
) -> list[SourceRecord]:
    if from_flk:
        records = search_sources(terms=terms, timeout_seconds=timeout_seconds)
    else:
        raise ValueError("A real source must be selected. Use --from-flk for phase 1 ingestion.")
    return records[:limit] if limit else records
