from pathlib import Path

from law_agent.review.cli import main


def test_review_cli_index_service_writes_index_artifacts(
    tmp_path: Path,
    capsys,
) -> None:
    from law_agent.data.io import write_jsonl
    from tests.test_review_retrieval_keyword import FIXTURE_CHUNKS

    chunks_path = tmp_path / "chunks.jsonl"
    es_path = tmp_path / "es_bulk.ndjson"
    pg_path = tmp_path / "pg_rows.jsonl"
    write_jsonl(chunks_path, FIXTURE_CHUNKS[:1])

    exit_code = main(
        [
            "index-service",
            "--chunks",
            str(chunks_path),
            "--elasticsearch-output",
            str(es_path),
            "--pgvector-output",
            str(pg_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Elasticsearch bulk" in captured.out
    assert es_path.exists()
    assert pg_path.exists()
    assert FIXTURE_CHUNKS[0].chunk_id in es_path.read_text(encoding="utf-8")
    assert FIXTURE_CHUNKS[0].chunk_id in pg_path.read_text(encoding="utf-8")
