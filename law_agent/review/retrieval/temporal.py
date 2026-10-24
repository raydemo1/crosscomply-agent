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
        published = _parsed(chunk.publish_date)
        if published and published > as_of:
            continue
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
        published = _parsed(chunk.publish_date)
        effective = _parsed(chunk.effective_date)
        expires = _parsed(chunk.valid_to)
        # 缺失日期按保守策略保留：只有明确晚于 as_of 才排除。
        # 语料元数据并非条条完整，把无日期一概排除会静默丢掉大量可用证据。
        if published and published > as_of:
            continue
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
        # 仅在能确证存在更新版本（同 key 有更晚 effective）时降级引用；
        # 纯粹缺日期元数据的语料不应被整体剥夺引用资格
        superseded = bool(effective and key and effective < latest.get(key, date.min))
        filtered.append(
            hit.model_copy(update={"can_cite_clause": False})
            if superseded or chunk.law_status in {"repealed", "amended"} else hit
        )
    return filtered
