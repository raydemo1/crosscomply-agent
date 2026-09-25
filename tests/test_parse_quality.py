"""Focused tests for parse-quality evaluation and the PDF text-first routing."""

from __future__ import annotations

from pathlib import Path

import pytest

from law_agent.data import normalize as normalize_module
from law_agent.data.normalize import ParsedText, normalize_source
from law_agent.data.quality import evaluate_chunks, evaluate_text
from law_agent.data.schemas import SourceRecord
from law_agent.kb.ingestion import (
    ParseQualityError,
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