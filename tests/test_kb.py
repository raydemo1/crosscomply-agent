from pathlib import Path

from law_agent.data.io import read_jsonl, write_jsonl
from law_agent.data.schemas import Chunk, Document, IngestMeta, SourceRecord
from law_agent.kb.bootstrap import initialize_legacy_corpus
from law_agent.kb.service import InMemoryIndex, KnowledgeBase, make_stable_chunks


def _source(source_id: str = "law_001") -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        title="数据出境管理规定",
        source_url="https://example.test/law_001",
        source_site="example.test",
        doc_type="regulation",
        include_in_mvp=True,
    )


def _chunk(text: str, index: int = 0) -> Chunk:
    return Chunk(
        chunk_id=f"legacy:{index:04d}",
        doc_id="law_001",
        source_id="law_001",
        title="数据出境管理规定",
        text=text,
        chunk_index=index,
        source_url="https://example.test/law_001",
        char_count=len(text),
    )


def test_same_content_with_a_different_filename_is_skipped(tmp_path: Path) -> None:
    kb = KnowledgeBase(tmp_path, index=InMemoryIndex())
    source = _source()
    chunks = make_stable_chunks([_chunk("第一条 数据处理者应当遵守本规定。")], source)

    first = kb.ingest_prepared(source, "数据处理者应当遵守本规定。", chunks)
    second = kb.ingest_prepared(source, "数据处理者应当遵守本规定。", chunks)

    assert first.action == "added"
    assert second.action == "skipped_duplicate"
    assert second.embedded_chunks == 0


def test_update_reuses_embeddings_for_unchanged_chunks(tmp_path: Path) -> None:
    embedding_batches: list[list[str]] = []

    def embed(texts: list[str]) -> list[list[float]]:
        embedding_batches.append(texts)
        return [[float(index)] for index, _ in enumerate(texts)]

    kb = KnowledgeBase(tmp_path, index=InMemoryIndex(), embed_texts=embed)
    source = _source()
    kb.ingest_prepared(
        source,
        "第一条 原文。\n第二条 原文。",
        make_stable_chunks([_chunk("第一条 原文。"), _chunk("第二条 原文。", 1)], source),
    )

    result = kb.ingest_prepared(
        source,
        "第一条 原文。\n第二条 已更正。",
        make_stable_chunks([_chunk("第一条 原文。"), _chunk("第二条 已更正。", 1)], source),
    )

    assert result.action == "updated"
    assert result.cached_chunks == 1
    assert result.embedded_chunks == 1
    assert [len(batch) for batch in embedding_batches] == [2, 1]


def test_list_sources_and_remove_source_clears_active_artifacts(tmp_path: Path) -> None:
    index = InMemoryIndex()
    kb = KnowledgeBase(tmp_path, index=index)
    first = _source("law_001")
    second = _source("law_002")
    second = second.model_copy(update={"title": "个人信息管理规定"})
    first_raw = tmp_path / "first.txt"
    second_raw = tmp_path / "second.txt"
    first_raw.write_text("第一条 原文。", encoding="utf-8")
    second_raw.write_text("第一条 另一份原文。", encoding="utf-8")

    kb.ingest_prepared(first, "第一条 原文。", [_chunk("第一条 原文。")], raw_file=first_raw)
    kb.ingest_prepared(second, "第一条 另一份原文。", [_chunk("第一条 另一份原文。")], raw_file=second_raw)
    documents = [
        Document(
            doc_id=source.source_id, source_id=source.source_id, title=source.title,
            source_url=source.source_url, source_site=source.source_site,
            doc_type=source.doc_type, text=text,
            ingest_meta=IngestMeta(fetched_at="2026-01-01T00:00:00Z", parser="plain", parser_version="1"),
        )
        for source, text in [(first, "第一条 原文。"), (second, "第一条 另一份原文。")]
    ]
    write_jsonl(tmp_path / "documents.normalized.jsonl", documents)
    (tmp_path / "fetch_status.csv").write_text(
        "source_id,title\n"
        "law_001,数据出境管理规定\n"
        "law_002,个人信息管理规定\n",
        encoding="utf-8",
    )
    cleaned_text = tmp_path / "cleaned_texts" / "001_数据出境管理规定_law_001.md"
    cleaned_text.parent.mkdir()
    cleaned_text.write_text("第一条 原文。", encoding="utf-8")

    summaries = kb.list_sources()
    assert [(item.source.source_id, item.chunk_count, item.status) for item in summaries] == [
        ("law_002", 1, "ready"),
        ("law_001", 1, "ready"),
    ]
    assert [item.raw_format for item in summaries] == ["txt", "txt"]

    removed = kb.remove_source("law_001")

    assert removed.source.source_id == "law_001"
    assert [item.source.source_id for item in kb.list_sources()] == ["law_002"]
    assert not (tmp_path / "raw" / "law_001").exists()
    assert [document.source_id for document in read_jsonl(tmp_path / "documents.normalized.jsonl", Document)] == ["law_002"]
    assert "law_001" not in (tmp_path / "fetch_status.csv").read_text(encoding="utf-8")
    assert not cleaned_text.exists()
    assert all(key[0] != "law_001" for key in index.generations)
    assert "law_001" not in index.current


def test_initialization_merges_manifests_and_rekeys_legacy_chunks(tmp_path: Path) -> None:
    source = _source()
    header = ",".join(SourceRecord.model_fields)
    row = source.model_dump(mode="json")
    (tmp_path / "source_manifest.review.csv").write_text(
        header + "\n" + ",".join(str(row.get(key, "")) for key in SourceRecord.model_fields) + "\n",
        encoding="utf-8",
    )
    raw = tmp_path / "raw" / "legacy" / "input.txt"
    raw.parent.mkdir(parents=True)
    raw.write_text("第一条 原文。", encoding="utf-8")
    (tmp_path / "fetch_status.csv").write_text(
        "source_id,title,ok,path\n"
        "law_001,数据出境管理规定,True,old/raw/legacy/input.txt\n",
        encoding="utf-8",
    )
    document = Document(
        doc_id="law_001", source_id="law_001", title=source.title,
        source_url=source.source_url, source_site=source.source_site,
        doc_type=source.doc_type, text="第一条 原文。",
        ingest_meta=IngestMeta(fetched_at="2026-01-01T00:00:00Z", parser="plain", parser_version="1"),
    )
    write_jsonl(tmp_path / "documents.normalized.jsonl", [document])
    write_jsonl(tmp_path / "chunks.jsonl", [_chunk("第一条 原文。")])

    result = initialize_legacy_corpus(tmp_path, signature="test-v1")

    assert result == {"sources": 1, "chunks": 1, "raw_moves": 1}
    assert (tmp_path / "raw" / "law_001" / "source.txt").exists()
    migrated = (tmp_path / "chunks.jsonl").read_text(encoding="utf-8")
    assert "legacy:0000" not in migrated
    assert "law_001:" in migrated

    resumed = initialize_legacy_corpus(tmp_path, signature="test-v1")
    assert resumed["raw_moves"] == 0

    kb = KnowledgeBase(tmp_path, index=InMemoryIndex(), signature="test-v1")
    assert [record.source_id for record in kb.exact_matches("第一条 原文。")] == ["law_001"]
