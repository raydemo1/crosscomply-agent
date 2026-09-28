"""Focused tests for parse-quality evaluation and the PDF text-first routing."""

from __future__ import annotations

from pathlib import Path

import pytest

from law_agent.data import normalize as normalize_module
from law_agent.data.chunking.pipeline import chunk_document
from law_agent.data.normalize import ParsedText, normalize_source
from law_agent.data.quality import evaluate_chunks, evaluate_text
from law_agent.data.schemas import Chunk, Document, IngestMeta, SourceRecord
from law_agent.kb.ingestion import (
    ChunkCoverageError,
    ParseQualityError,
    check_chunk_coverage,
    prepare_chunks_for_publish,
    prepare_document_for_ingest,
)
from law_agent.kb.service import processing_signature

CLEAN_ARTICLE = "第一条 为了保护个人信息权益，规范个人信息处理活动，促进个人信息合理利用，制定本法。"
DAMAGED_NUMBER = "GB / T 4 3 6 9 7 - 2 0 2 4"
SPACED_ACRONYM = "D a t a s e c u r i t y"
TOC_LEADER = "前言 …………………………………………………………………………………… Ⅲ"
CORRUPTION = "\ufffd"


def _source_record(source_id: str = "upload_001") -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        title="用户上传标准",
        source_url="file:///upload.pdf",
        source_site="user_upload",
        doc_type="guideline",
        file_format="pdf",
        include_in_mvp=True,
    )


def _clean_body(paragraphs: int = 4) -> str:
    return "\n\n".join(f"第{n}条 " + CLEAN_ARTICLE[4:] for n in "一二三四五"[:paragraphs])


@pytest.mark.parametrize(
    ("sample", "expected_code"),
    [
        (DAMAGED_NUMBER, "spaced_digits"),
        (SPACED_ACRONYM, "spaced_latin"),
        ("T C2 6 0 术语与定义", "mixed_fragment"),
        ("见 GB / T 43697 的规定", "broken_identifier"),
        (TOC_LEADER, "toc_leader"),
        (f"替换字符{CORRUPTION}出现在正文中", "corruption_chars"),
    ],
)
def test_detector_flags_parser_artifact_classes(sample: str, expected_code: str) -> None:
    result = evaluate_text(f"{sample}\n{sample}\n{sample}", min_chars=1)

    assert expected_code in result.codes


def test_detector_leaves_clean_text_alone() -> None:
    result = evaluate_text(_clean_body() + "\n本标准适用于 GB/T 43697-2024 的规定。", min_chars=1)

    assert result.status == "ok"
    assert result.issues == []


def test_short_body_is_not_treated_as_damage() -> None:
    result = evaluate_text("数据出境办事指南\n申请材料以官方页面为准。", min_chars=1)

    assert result.status == "ok"


def test_regulation_with_appended_interpretation_is_rejected(tmp_path: Path) -> None:
    raw = tmp_path / "source.md"
    raw.write_text(
        "第一条 本条例适用于本市数据处理活动。\n"
        "第二条 数据处理者应当依法处理数据。\n"
        "《深圳经济特区 数据 条例》解读\n"
        "市人大常委会法工委\n",
        encoding="utf-8",
    )
    source = _source_record().model_copy(update={
        "doc_type": "regulation", "file_format": "md",
    })

    with pytest.raises(ValueError, match="法条正文后混入独立解读"):
        prepare_document_for_ingest(raw, parser="plain", source=source)


def test_contents_leaders_and_single_char_ratio_only_warn() -> None:
    leaders = evaluate_text(f"{TOC_LEADER}\n" * 12, min_chars=1)
    soup = evaluate_text(" ".join("数" for _ in range(80)), min_chars=1)

    assert leaders.status == "warn"
    assert "toc_leader" in leaders.codes
    assert soup.status == "warn"
    assert "single_char_tokens" in soup.codes


def test_pdf_with_healthy_text_layer_never_reaches_ocr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_path = tmp_path / "upload_001.pdf"
    raw_path.write_bytes(b"%PDF-1.4")
    body = _clean_body()
    ocr_calls: list[bool] = []

    monkeypatch.setattr(
        normalize_module,
        "_pdf_native_text",
        lambda path: ParsedText(body, "pdf_text_parser", "test", strategy="native_text"),
    )
    monkeypatch.setattr(normalize_module, "_docling_available", lambda: True)

    def fake_docling(path: Path, *, ocr: bool, strategy: str | None = None) -> ParsedText:
        ocr_calls.append(ocr)
        return ParsedText(body, "docling_parser", "test", ocr_used=ocr, strategy=strategy or "")

    monkeypatch.setattr(normalize_module, "_docling_to_text", fake_docling)

    document = normalize_source(_source_record(), raw_path)

    assert ocr_calls == [False]
    assert document.ingest_meta.ocr_used is False
    assert document.ingest_meta.strategy == "docling_no_ocr"


def test_pdf_without_text_layer_falls_back_to_ocr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_path = tmp_path / "slice_001.pdf"
    raw_path.write_bytes(b"%PDF-1.4")
    ocr_calls: list[bool] = []

    def unreadable(path: Path) -> ParsedText:
        raise RuntimeError("no embedded text layer")

    monkeypatch.setattr(normalize_module, "_pdf_native_text", unreadable)

    def fake_docling(path: Path, *, ocr: bool, strategy: str | None = None) -> ParsedText:
        ocr_calls.append(ocr)
        return ParsedText(_clean_body(), "docling_parser", "test", ocr_used=ocr, strategy=strategy or "")

    monkeypatch.setattr(normalize_module, "_docling_to_text", fake_docling)

    document = normalize_source(_source_record("upload_002"), raw_path)

    assert ocr_calls == [True]
    assert document.ingest_meta.ocr_used is True
    assert document.ingest_meta.strategy == "docling_ocr"


def test_degraded_structured_parse_falls_back_to_native_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_path = tmp_path / "upload_003.pdf"
    raw_path.write_bytes(b"%PDF-1.4")
    body = _clean_body()
    degraded = f"{body}\n{DAMAGED_NUMBER}\n"

    monkeypatch.setattr(
        normalize_module,
        "_pdf_native_text",
        lambda path: ParsedText(body, "pdf_text_parser", "test", strategy="native_text"),
    )
    monkeypatch.setattr(normalize_module, "_docling_available", lambda: True)
    monkeypatch.setattr(
        normalize_module,
        "_docling_to_text",
        lambda path, *, ocr, strategy=None: ParsedText(
            degraded, "docling_parser", "test", ocr_used=ocr, strategy=strategy or ""
        ),
    )

    document = normalize_source(_source_record("upload_003"), raw_path)

    assert document.text == body
    assert document.ingest_meta.parser == "pdf_text_parser"
    assert document.ingest_meta.strategy == "native_text_fallback"
    assert document.ingest_meta.ocr_used is False


def test_ingest_records_parser_route_and_quality_provenance(tmp_path: Path) -> None:
    raw_path = tmp_path / "guide.txt"
    raw_path.write_text(_clean_body(), encoding="utf-8")

    document = prepare_document_for_ingest(raw_path, parser="auto")

    assert document.ingest_meta.parser == "plain_text_parser"
    assert document.ingest_meta.strategy == "plain"
    assert document.ingest_meta.ocr_used is False
    assert document.ingest_meta.quality_status == "ok"
    assert document.ingest_meta.quality_issues == []


def test_document_gate_refuses_a_body_that_is_still_corrupted(tmp_path: Path) -> None:
    raw_path = tmp_path / "damaged.txt"
    raw_path.write_text(
        f"{CLEAN_ARTICLE}\n{CORRUPTION} {CORRUPTION} {CORRUPTION} 解码失败的正文字节。",
        encoding="utf-8",
    )

    with pytest.raises(ParseQualityError) as excinfo:
        prepare_document_for_ingest(raw_path, parser="auto")

    assert "corruption_chars" in excinfo.value.result.codes


def test_chunk_gate_is_not_masked_by_clean_neighbours() -> None:
    assert evaluate_chunks([CLEAN_ARTICLE] * 3).status == "ok"
    assert evaluate_chunks([CLEAN_ARTICLE, CLEAN_ARTICLE, f"第二条 处理目的{CORRUPTION}不明确。"]).status == "fail"


def test_publish_gate_refuses_a_document_whose_clause_is_damaged(tmp_path: Path) -> None:
    raw_path = tmp_path / "clauses.txt"
    raw_path.write_text(_clean_body(), encoding="utf-8")
    document = prepare_document_for_ingest(raw_path, parser="auto")

    assert len(prepare_chunks_for_publish(document)) > 0

    damaged = document.model_copy(
        update={"text": document.text + f"\n第六条 处理目的{CORRUPTION}不明确。"}
    )
    with pytest.raises(ParseQualityError) as excinfo:
        prepare_chunks_for_publish(damaged)

    assert "corruption_chars" in excinfo.value.result.codes


@pytest.mark.parametrize(
    "override",
    [
        {"parser_version": "other-parser"},
        {"cleaning_version": "other-cleaning"},
        {"chunking_version": "other-chunking"},
    ],
)
def test_processing_signature_changes_with_the_pipeline(override: dict[str, str]) -> None:
    base = processing_signature(embedding_model="m", embedding_dimension=8)

    assert base != processing_signature(embedding_model="m", embedding_dimension=8, **override)


def _document(text: str, *, doc_type: str = "guideline") -> Document:
    return Document.model_validate(
        {
            "doc_id": "doc",
            "source_id": "doc",
            "title": "用户上传文件",
            "source_url": "file:///upload.html",
            "source_site": "user_upload",
            "doc_type": doc_type,
            "text": text,
            "ingest_meta": IngestMeta(
                fetched_at="2026-07-01T00:00:00Z",
                parser="test_parser",
                parser_version="0.1.0",
            ),
        }
    )


def test_coverage_reports_full_retention_for_a_reformatted_table() -> None:
    """A repeated header and re-piped cells are layout, not lost body text."""

    document = _document(
        "| 数据类别 | 判定规则 |\n"
        "| --- | --- |\n"
        + "| 重要数据 | 向境外提供前应当申报安全评估。 |\n" * 2
    )

    chunks = chunk_document(document)
    coverage = check_chunk_coverage(document, chunks)

    assert coverage.blocks_publish is False
    assert coverage.missing == []
    assert coverage.ratio == 1.0


def test_coverage_detects_a_whole_block_that_never_reached_a_chunk() -> None:
    lost_block = "第六条 " + "数据处理者应当开展数据出境风险自评估。" * 20
    document = _document(
        "## 1 范围\n"
        "第一条 为了保护个人信息权益，制定本法。\n"
        "## 2 数据出境风险自评估\n"
        f"{lost_block}\n"
    )
    chunks = chunk_document(document)

    assert check_chunk_coverage(document, chunks).blocks_publish is False

    damaged = [chunk for chunk in chunks if "风险自评估" not in chunk.text]
    coverage = check_chunk_coverage(document, damaged)

    assert coverage.blocks_publish is True
    assert coverage.largest_gap_chars >= 200
    assert coverage.missing[0].line_no == 3


def test_repeated_table_header_does_not_offset_a_missing_row() -> None:
    row = (
        "<tr><td>个人信息</td><td>"
        + "自当年1月1日起累计向境外提供100万人以上个人信息，应当申报数据出境安全评估。" * 4
        + "</td></tr>"
    )
    document = _document(
        "<table><tr><th>数据类别</th><th>判定规则</th></tr>" + row * 3 + "</table>"
    )
    chunks = chunk_document(document)

    damaged = [chunk for chunk in chunks if "100万人以上" not in chunk.text]
    coverage = check_chunk_coverage(document, damaged)

    assert coverage.blocks_publish is True
    assert coverage.missing_chars > 0


def test_coverage_keeps_a_table_split_across_several_chunks() -> None:
    """A table row longer than one chunk is stored as pieces, header repeated.

    No single span of the published text then equals the body line, so coverage
    has to consume the pieces in order rather than match the line from either
    end — the negative-list tables in the corpus are exactly this shape.
    """

    filler = "自当年1月1日起累计向境外提供100万人以上个人信息，应当申报数据出境安全评估。" * 9
    rows = "".join(
        f"<tr><td>数据类别 {index}</td><td>{filler}</td></tr>" for index in range(4)
    )
    document = _document(
        "<table><tr><th>数据类别</th><th>判定规则</th></tr>" + rows + "</table>"
    )
    chunks = chunk_document(document)

    assert len(chunks) >= 3, "the fixture has to split the table across chunks"
    coverage = check_chunk_coverage(document, chunks)

    assert coverage.missing == []
    assert coverage.blocks_publish is False


def test_coverage_gate_blocks_a_deleted_obligation_however_short() -> None:
    """A deleted exception clause is short by nature, so size cannot excuse it."""

    obligation = "（二）法律、行政法规规定的其他情形。"
    document = _document(
        "## 1 范围\n"
        "第一条 为了保护个人信息权益，制定本法。\n"
        "## 2 例外\n"
        f"{obligation}\n"
    )
    chunks = chunk_document(document)
    assert check_chunk_coverage(document, chunks).blocks_publish is False

    damaged = [
        chunk.model_copy(update={"text": chunk.text.replace(obligation, "")})
        for chunk in chunks
    ]
    coverage = check_chunk_coverage(document, damaged)

    assert coverage.blocks_publish is True
    assert coverage.missing_chars < 40
    assert obligation in coverage.missing[0].text


def test_coverage_notices_when_one_copy_of_repeated_text_is_lost() -> None:
    """Only the first copy is in the chunks; the second is still missing.

    This is what ordering buys: the second copy cannot be credited to the first
    one, because the cursor has already moved past it.
    """

    second = "（一）数据处理者应当申报数据出境安全评估。"
    document = _document(
        "## 1 范围\n"
        "第一条 数据处理者应当申报数据出境安全评估。\n"
        "## 2 例外\n"
        f"{second}\n"
    )
    chunks = chunk_document(document)
    assert check_chunk_coverage(document, chunks).blocks_publish is False

    damaged = [
        chunk.model_copy(update={"text": chunk.text.replace(second, "")})
        for chunk in chunks
    ]
    coverage = check_chunk_coverage(document, damaged)

    assert coverage.blocks_publish is True
    assert second in coverage.missing[0].text


def test_coverage_refuses_chunks_published_out_of_order() -> None:
    """Every line survives, but the body was re-ordered on the way out."""

    document = _document(
        "## 1 范围\n"
        "第一条 为了保护个人信息权益，制定本法。\n"
        "## 2 报告\n"
        "第二条 数据处理者应当每年开展风险评估。\n"
    )
    chunks = chunk_document(document)
    assert check_chunk_coverage(document, chunks).blocks_publish is False

    coverage = check_chunk_coverage(document, list(reversed(chunks)))

    assert coverage.blocks_publish is True
    assert "第二条 数据处理者应当每年开展风险评估。" in coverage.missing[0].text


def test_coverage_does_not_credit_a_line_that_only_fragments_a_heading() -> None:
    """The heading paths are the one other place a body line may survive — but
    only a line that *is* a heading survives there.

    "第一节" is a fragment of the heading "第一节 一般规定". Crediting any
    fragment of any heading let a short line the chunker never published pass
    the check that exists to catch exactly that.
    """

    document = _document(
        "第一章 总则\n"
        "第一节 一般规定\n"
        f"{CLEAN_ARTICLE}\n"
        "第一节\n"
    )
    published = Chunk(
        chunk_id="c1",
        doc_id="doc",
        source_id="doc",
        title="用户上传文件",
        text=CLEAN_ARTICLE,
        chunk_index=0,
        heading_path=["第一章 总则", "第一节 一般规定", "第一条"],
        source_url="file:///upload.html",
        char_count=len(CLEAN_ARTICLE),
    )

    coverage = check_chunk_coverage(document, [published])

    assert [gap.text for gap in coverage.missing] == ["第一节"]
    assert coverage.blocks_publish is True


def test_coverage_keeps_checking_after_a_references_section() -> None:
    """References are excluded by heading, so the exclusion ends with it.

    Treating every later line as references also excluded a closing appendix,
    which then escaped the check that exists to catch dropped text.
    """

    obligation = "（二）法律、行政法规规定的其他情形。"
    document = _document(
        "## 1 范围\n"
        f"{CLEAN_ARTICLE}\n"
        "## 参考文献\n"
        "[1] 中华人民共和国个人信息保护法.\n"
        "## 附录 例外情形\n"
        f"{obligation}\n"
    )
    chunks = chunk_document(document)
    assert check_chunk_coverage(document, chunks).blocks_publish is False

    damaged = [
        chunk.model_copy(update={"text": chunk.text.replace(obligation, "")})
        for chunk in chunks
    ]
    coverage = check_chunk_coverage(document, damaged)

    assert coverage.blocks_publish is True
    assert obligation in coverage.missing[0].text


def test_coverage_gate_refuses_publishing_when_no_chunk_survives() -> None:
    document = _document("   \n\n   \n")

    with pytest.raises(ChunkCoverageError) as excinfo:
        prepare_chunks_for_publish(document)

    assert excinfo.value.coverage.chunk_count == 0
