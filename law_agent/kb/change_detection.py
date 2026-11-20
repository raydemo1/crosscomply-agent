"""Diff two governed versions of a source without interpreting the law."""

from dataclasses import dataclass
from typing import Any, Literal

from law_agent.data.schemas import Chunk, SourceRecord
from law_agent.kb.service import _chunk_locator
from law_agent.review.retrieval.temporal import instrument_identity


@dataclass(frozen=True)
class SourceChange:
    source_id: str
    instrument_key: str
    change_type: Literal["added", "body_changed", "metadata_changed", "removed", "restored"]
    detail: dict[str, Any]


def diff_source(
    before_source: SourceRecord | None, before_chunks: list[Chunk],
    after_source: SourceRecord | None, after_chunks: list[Chunk],
) -> SourceChange | None:
    source = after_source or before_source
    if source is None or source.authority == "simulated_internal_policy":
        return None
    before = before_source.model_dump(mode="json") if before_source else {}
    after = after_source.model_dump(mode="json") if after_source else {}
    differences = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    before_by_id = {row.chunk_id: row for row in before_chunks}
    after_by_id = {row.chunk_id: row for row in after_chunks}
    removed = sorted(before_by_id.keys() - after_by_id.keys())
    added = sorted(after_by_id.keys() - before_by_id.keys())
    if before_source is None:
        kind = "added"
    elif after_source is None:
        kind = "removed"
    elif removed or added:
        kind = "body_changed"
    elif differences:
        kind = "metadata_changed"
    else:
        return None
    new_locations: dict[str, list[Chunk]] = {}
    for identifier in added:
        row = after_by_id[identifier]
        new_locations.setdefault(_chunk_locator(row), []).append(row)
    changed_articles = []
    for identifier in removed:
        old = before_by_id[identifier]
        matches = new_locations.get(_chunk_locator(old))
        if matches:
            new = matches.pop(0)
            changed_articles.append({"article_no": old.article_no, "old_chunk_id": identifier, "new_chunk_id": new.chunk_id})
    labels = {"title", "source_url", "download_url", "source_site"}
    return SourceChange(source.source_id, instrument_identity(source), kind, {
        "title": source.title, "before": before or None, "after": after or None,
        "removed_chunk_ids": removed, "changed_articles": changed_articles,
        "status_changes": {key: {"before": before.get(key), "after": after.get(key)}
                           for key in ("law_status", "effective_date", "valid_to") if key in differences},
        "title_or_url_changed": bool(differences & labels),
        "metadata_only": kind == "metadata_changed" and differences <= labels,
    })
