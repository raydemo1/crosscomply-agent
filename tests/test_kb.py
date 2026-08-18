from pathlib import Path

from law_agent.data.schemas import Chunk, SourceRecord
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
    kb.ingest_prepared(
        second, "第一条 另一份原文。", [_chunk("第一条 另一份原文。")], raw_file=second_raw
    )
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
    assert all(key[0] != "law_001" for key in index.generations)
    assert "law_001" not in index.current
