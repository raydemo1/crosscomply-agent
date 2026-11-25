from law_agent.review.retrieval.packets import (
    build_source_evidence_packets,
    flatten_source_evidence_packets,
)
from law_agent.review.schemas import RetrievalHit


def _hit(chunk_id: str, source_id: str, rank: int) -> RetrievalHit:
    return RetrievalHit(chunk_id=chunk_id, doc_id=source_id, source_id=source_id, title=source_id, text="正文",
                        source_url="https://example.test", rank=rank, score=1.0 - rank / 10, retriever="hybrid",
                        citation_role="primary_legal_basis", can_cite_clause=True)


def test_source_evidence_packets_keep_same_source_supporting_chunks() -> None:
    representative = _hit("a1", "source_a", 0)
    supporting = [_hit("a2", "source_a", 1), _hit("a3", "source_a", 2), _hit("b1", "source_b", 3)]
    neighbor = _hit("a0", "source_a", 4)

    packets = build_source_evidence_packets(
        representative_hits=[representative], candidate_hits=[representative, *supporting], neighbor_hits=[neighbor],
    )

    assert len(packets) == 1
    assert packets[0].representative_chunk.chunk_id == "a1"
    assert [hit.chunk_id for hit in packets[0].supporting_chunks] == ["a2", "a3"]
    assert [hit.chunk_id for hit in packets[0].neighbor_chunks] == ["a0"]
    assert [hit.chunk_id for hit in flatten_source_evidence_packets(packets)] == ["a1", "a2", "a3", "a0"]
