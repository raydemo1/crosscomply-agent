import json
from pathlib import Path

from law_agent.external.legalbench_rag.data import (
    build_chunks,
    load_documents_for_queries,
    load_mini_queries,
)
from law_agent.external.legalbench_rag.metrics import evaluate_case
from law_agent.external.legalbench_rag.schemas import (
    LegalBenchChunkMeta,
    LegalBenchDocument,
    LegalBenchQuery,
    LegalBenchSnippet,
)
from law_agent.review.schemas import RetrievalHit


def test_build_chunks_preserves_character_ranges() -> None:
    documents = [LegalBenchDocument(file_path="contracts/a.txt", text="abcdefghijklmnopqrstuvwxyz")]

    chunks, metas = build_chunks(documents, chunk_size=10, overlap=2)

    assert [meta.char_start for meta in metas] == [0, 8, 16]
    assert [meta.char_end for meta in metas] == [10, 18, 26]
    assert chunks[0].next_chunk_id == chunks[1].chunk_id
    assert chunks[1].prev_chunk_id == chunks[0].chunk_id
    assert chunks[0].source_id == "contracts/a.txt"


def test_evaluate_case_tracks_doc_and_span_hits() -> None:
    query = LegalBenchQuery(
        query_id="q1",
        query="Where is the assignment clause?",
        snippets=[LegalBenchSnippet(file_path="contracts/a.txt", span=(20, 40))],
        tags=["cuad"],
    )
    meta_by_id = {
        "chunk_1": LegalBenchChunkMeta(
            chunk_id="chunk_1",
            file_path="contracts/b.txt",
            char_start=0,
            char_end=100,
        ),
        "chunk_2": LegalBenchChunkMeta(
            chunk_id="chunk_2",
            file_path="contracts/a.txt",
            char_start=10,
            char_end=30,
        ),
    }
    hits = [
        _hit("chunk_1", source_id="contracts/b.txt", rank=0),
        _hit("chunk_2", source_id="contracts/a.txt", rank=1),
    ]

    result = evaluate_case(query, hits, meta_by_id, latency_ms=12)

    assert result.doc_hit_rank == 2
    assert result.span_hit_rank == 2
    assert result.doc_recall_at_5 is True
    assert result.span_recall_at_5 is True
    assert result.best_span_overlap == 0.5
    assert result.latency_ms == 12


def test_load_mini_queries_matches_upstream_sampling_shape(tmp_path: Path) -> None:
    benchmarks = tmp_path / "benchmarks"
    corpus = tmp_path / "corpus"
    benchmarks.mkdir()
    corpus.mkdir()
    for name in ("privacy_qa", "contractnli", "maud", "cuad"):
        payload = {
            "tests": [
                {
                    "query": f"{name} question {index}",
                    "snippets": [{"file_path": f"{name}.txt", "span": [0, 5]}],
                }
                for index in range(3)
            ]
        }
        (benchmarks / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")
        (corpus / f"{name}.txt").write_text("hello world", encoding="utf-8")

    queries = load_mini_queries(tmp_path, max_tests_per_benchmark=2)
    documents = load_documents_for_queries(tmp_path, queries)

    assert len(queries) == 8
    assert {query.tags[0] for query in queries} == {
        "privacy_qa",
        "contractnli",
        "maud",
        "cuad",
    }
    assert len(documents) == 4


def _hit(chunk_id: str, *, source_id: str, rank: int) -> RetrievalHit:
    return RetrievalHit(
        chunk_id=chunk_id,
        doc_id=source_id.replace("/", "_"),
        source_id=source_id,
        title=source_id,
        text="text",
        score=1.0,
        rank=rank,
        retriever="hybrid",
        citation_role="primary_legal_basis",
        can_cite_clause=True,
        source_url=f"legalbenchrag://{source_id}",
        matched_query_type="legal_issue",
    )
