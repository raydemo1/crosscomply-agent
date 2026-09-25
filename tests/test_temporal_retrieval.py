from datetime import date

from law_agent.data.schemas import Chunk
from law_agent.review.retrieval.temporal import filter_hits_as_of
from law_agent.review.schemas import RetrievalHit


def pair(source_id: str, effective_date: str, valid_to: str | None = None):
    chunk = Chunk(
        chunk_id=source_id, doc_id=source_id, source_id=source_id,
        title="示例法", text="第一条 示例规定。", chunk_index=0,
        source_url="https://flk.npc.gov.cn/example", char_count=10,
        authority="national_law", law_status="effective",
        citation_role="primary_legal_basis", can_cite_clause=True,
        effective_date=effective_date, valid_to=valid_to,
    )
    hit = RetrievalHit(
        chunk_id=source_id, doc_id=source_id, source_id=source_id,
        title=chunk.title, text=chunk.text, score=1.0, rank=0,
        retriever="keyword", citation_role="primary_legal_basis",
        can_cite_clause=True, source_url=chunk.source_url,
    )
    return chunk, hit


def test_latest_effective_version_is_selected_without_deleting_history() -> None:
    old, old_hit = pair("old", "2020-01-01")
    new, new_hit = pair("new", "2026-10-01")
    chunks = {"old": old, "new": new}
    assert filter_hits_as_of([old_hit, new_hit], chunks, as_of=date(2026, 9, 25)) == [old_hit]
    assert filter_hits_as_of([old_hit, new_hit], chunks, as_of=date(2026, 10, 2)) == [new_hit]


def test_explicit_valid_to_excludes_expired_version() -> None:
    old, hit = pair("old", "2020-01-01", valid_to="2024-01-01")
    assert filter_hits_as_of([hit], {"old": old}, as_of=date(2024, 1, 1)) == []


def test_new_version_shares_legacy_title_group() -> None:
    old, old_hit = pair("old", "2020-01-01")
    new, new_hit = pair("new", "2026-10-01")
    new = new.model_copy(update={"instrument_key": "示例法"})
    chunks = {"old": old, "new": new}
    assert filter_hits_as_of([old_hit, new_hit], chunks, as_of=date(2026, 9, 25)) == [old_hit]
    assert filter_hits_as_of([old_hit, new_hit], chunks, as_of=date(2026, 10, 2)) == [new_hit]
