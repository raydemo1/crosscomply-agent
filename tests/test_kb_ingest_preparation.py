from pathlib import Path

from law_agent.data.chunking.pipeline import chunk_document
from law_agent.kb.cli import _prepare_document_for_ingest


def test_ingest_preparation_cleans_before_law_chunking(tmp_path: Path) -> None:
    """The normal ingest preparation path must not chunk page-wrapper noise."""

    source = tmp_path / "深圳经济特区数据条例.md"
    source.write_text(
        "Title: 《深圳经济特区数据条例》全文公布！\n"
        "URL Source: https://example.test/regulation\n"
        "Markdown Content:\n"
        "## 目 录\n"
        "## 第一章 总则\n"
        "## 第二章 个人数据\n"
        "## 第一章 总则\n"
        "## 第一条 为了规范数据处理活动，保护自然人合法权益。\n"
        "## 第二条 本条例中下列用语的含义，按照国家有关规定和本条例执行。\n",
        encoding="utf-8",
    )

    document = _prepare_document_for_ingest(source, parser="plain")
    final_document = document.model_copy(update={"doc_type": "regulation"})
    chunks = chunk_document(final_document)

    assert "Title:" not in document.text
    assert "URL Source:" not in document.text
    assert "目 录" not in document.text
    assert [chunk.article_no for chunk in chunks] == ["第一条", "第二条"]
