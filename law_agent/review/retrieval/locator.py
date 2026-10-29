"""Resolve explicit legal source titles within the controlled corpus."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date

from law_agent.data.schemas import Chunk
from law_agent.review.retrieval.neighbors import hit_from_chunk
from law_agent.review.retrieval.temporal import filter_hits_as_of


def source_title_key(title: str) -> str:
    return "".join(title.split()).removeprefix("《").removesuffix("》").removeprefix("中华人民共和国")


def resolve_source_title(title: str, chunks_by_id: Mapping[str, Chunk], *, as_of: date) -> str:
    key = source_title_key(title)
    if not key:
        raise ValueError("法源名称不能为空")
    matches = [hit_from_chunk(chunk, 0) for chunk in chunks_by_id.values()
               if source_title_key(chunk.title) == key]
    available = filter_hits_as_of(matches, chunks_by_id, as_of=as_of)
    sources = sorted({hit.source_id for hit in available})
    if not sources:
        raise ValueError(f"受控法律库在 {as_of.isoformat()} 没有名称为《{title}》的可用来源；请用 search_evidence 查找正式名称")
    if len(sources) > 1:
        raise ValueError(f"《{title}》在 {as_of.isoformat()} 对应多个来源，不能唯一定位；请先检索核对来源：{sources}")
    return sources[0]
