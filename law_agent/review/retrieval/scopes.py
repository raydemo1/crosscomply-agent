"""Query-level retrieval scopes derived from corpus metadata.

Default ``全国通用`` keeps national primary laws, standards, guides and FAQs free
from regional and industry crowding. Selecting a ``地区:*`` or ``行业:*`` scope
overlays that region or industry on top of ``全国通用`` so superior national
rules and national CAC policy Q&As remain available alongside specialized rules.
"""

from __future__ import annotations

from collections.abc import Sequence

from law_agent.data.schemas import Chunk
from law_agent.review.retrieval.boosts import (
    _INDUSTRY_KEYWORD_MAP,
    _REGION_CODE_MAP,
    _industry_matches,
    _is_specific_region,
    _normalize_region,
    _region_matches,
)

SCOPE_NATIONAL = "全国通用"
SCOPE_ALL = "全库"
LOCAL_SCOPE_PREFIX = "地区:"
INDUSTRY_SCOPE_PREFIX = "行业:"
GBA_HK_LABEL = "粤港澳大湾区（香港）"
GBA_MO_LABEL = "粤港澳大湾区（澳门）"
GBA_ALL_LABEL = "粤港澳大湾区"

_CODE_TO_REGION_NAME: dict[str, str] = {code: name for name, code in _REGION_CODE_MAP.items()}
_GBA_LABELS: dict[str, str] = {
    "CN-GBA-HK": GBA_HK_LABEL,
    "CN-GBA-MO": GBA_MO_LABEL,
}
_GBA_LABEL_TO_CODE: dict[str, str] = {label: code for code, label in _GBA_LABELS.items()}


def _is_gba_region(region: str) -> bool:
    return _normalize_region(region).startswith("CN-GBA")


def _region_label_for_chunk(chunk: Chunk) -> str | None:
    region = _normalize_region(chunk.applicable_region)
    if not _is_specific_region(region):
        return None
    if region in _GBA_LABELS:
        return _GBA_LABELS[region]
    if _is_gba_region(region):
        return GBA_ALL_LABEL
    return _CODE_TO_REGION_NAME.get(region, region)


def _is_national_general(chunk: Chunk) -> bool:
    chunk_region = _normalize_region(chunk.applicable_region)
    return not _is_specific_region(chunk_region) and chunk.citation_role != "conditional_industry_basis"


def available_scopes(chunks: Sequence[Chunk]) -> list[str]:
    """Build the ordered list of non-empty retrieval scope enum values from a corpus."""

    regions: list[str] = []
    seen_regions: set[str] = set()
    for name in _REGION_CODE_MAP:
        if any(_region_label_for_chunk(chunk) == name for chunk in chunks):
            regions.append(name)
            seen_regions.add(name)
    for label in (GBA_HK_LABEL, GBA_MO_LABEL):
        if any(_region_label_for_chunk(chunk) == label for chunk in chunks):
            regions.append(label)
            seen_regions.add(label)
    for chunk in chunks:
        label = _region_label_for_chunk(chunk)
        if label and label not in seen_regions:
            regions.append(label)
            seen_regions.add(label)

    industries = [
        industry
        for industry in _INDUSTRY_KEYWORD_MAP
        if any(
            not _is_specific_region(chunk.applicable_region)
            and chunk.citation_role == "conditional_industry_basis"
            and _industry_matches(chunk, industry)
            for chunk in chunks
        )
    ]

    return [
        SCOPE_NATIONAL,
        *(f"{LOCAL_SCOPE_PREFIX}{region}" for region in regions),
        *(f"{INDUSTRY_SCOPE_PREFIX}{industry}" for industry in industries),
        SCOPE_ALL,
    ]


def validate_scope(scope: str) -> str:
    """Validate syntax of a query scope value."""

    value = scope.strip()
    if value in {SCOPE_NATIONAL, SCOPE_ALL}:
        return value
    if value.startswith(LOCAL_SCOPE_PREFIX) and value.removeprefix(LOCAL_SCOPE_PREFIX).strip():
        return f"{LOCAL_SCOPE_PREFIX}{value.removeprefix(LOCAL_SCOPE_PREFIX).strip()}"
    if value.startswith(INDUSTRY_SCOPE_PREFIX) and value.removeprefix(INDUSTRY_SCOPE_PREFIX).strip():
        return f"{INDUSTRY_SCOPE_PREFIX}{value.removeprefix(INDUSTRY_SCOPE_PREFIX).strip()}"
    raise ValueError(
        f"无效的检索域 scope={scope!r}；须为 {SCOPE_NATIONAL!r}、{SCOPE_ALL!r}、"
        f"{LOCAL_SCOPE_PREFIX!r}<地区> 或 {INDUSTRY_SCOPE_PREFIX!r}<行业>"
    )


def chunk_in_scope(chunk: Chunk, scope: str) -> bool:
    """Return whether ``chunk`` belongs to the requested retrieval scope.

    ``地区:*`` and ``行业:*`` overlay their specialized documents on top of
    ``全国通用`` so superior national laws and national policy Q&As remain
    searchable while unrelated regions and industries stay excluded.
    """

    normalized = validate_scope(scope)
    if normalized == SCOPE_ALL:
        return True
    if _is_national_general(chunk):
        return True
    if normalized == SCOPE_NATIONAL:
        return False

    chunk_region = _normalize_region(chunk.applicable_region)
    is_local = _is_specific_region(chunk_region)
    if normalized.startswith(LOCAL_SCOPE_PREFIX):
        if not is_local:
            return False
        target = normalized.removeprefix(LOCAL_SCOPE_PREFIX)
        if target in _GBA_LABEL_TO_CODE:
            return chunk_region == _GBA_LABEL_TO_CODE[target]
        if target == GBA_ALL_LABEL:
            return _is_gba_region(chunk_region)
        if target.startswith("CN-GBA"):
            return chunk_region == target or (target == "CN-GBA" and _is_gba_region(chunk_region))
        target_code = _normalize_region(target)
        if target_code in {"CN-GD", "CN-GD-SZ"} and _is_gba_region(chunk_region):
            return True
        return _region_matches(chunk_region, target_code)

    target_industry = normalized.removeprefix(INDUSTRY_SCOPE_PREFIX)
    return (
        not is_local
        and chunk.citation_role == "conditional_industry_basis"
        and _industry_matches(chunk, target_industry)
    )


def catalog_sources_for_scope(chunks: Sequence[Chunk], scope: str) -> list[dict[str, str]]:
    """Return a compact list of key sources in ``scope`` so the Agent knows what exists.

    - For ``全国通用``: lists formal citable national laws/regulations (sorted newest first),
      making recent rules (e.g. 2026 simplified measures) discoverable even if outside model pretraining.
    - For ``地区:*`` / ``行业:*``: lists the specialized sources layered onto that scope (1–6 titles).
    - For ``全库``: returns empty (avoid dumping the entire corpus catalog).
    """

    normalized = validate_scope(scope)
    if normalized == SCOPE_ALL:
        return []
    grouped: dict[str, list[Chunk]] = {}
    for chunk in chunks:
        grouped.setdefault(chunk.source_id, []).append(chunk)

    entries: list[dict[str, str]] = []
    for source_id, source_chunks in grouped.items():
        rep = source_chunks[0]
        if normalized == SCOPE_NATIONAL:
            if not (_is_national_general(rep) and any(c.can_cite_clause for c in source_chunks)):
                continue
        else:
            if _is_national_general(rep) or not chunk_in_scope(rep, normalized):
                continue
        entry: dict[str, str] = {"source_id": source_id, "title": rep.title}
        if rep.publish_date:
            entry["publish_date"] = rep.publish_date
        entries.append(entry)

    entries.sort(key=lambda item: (item.get("publish_date", ""), item["source_id"]), reverse=True)
    return entries
