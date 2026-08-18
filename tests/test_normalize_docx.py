import subprocess
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from law_agent.data import normalize as normalize_module
from law_agent.data.normalize import ParsedText, _docx_to_text, normalize_source
from law_agent.data.schemas import SourceRecord


def _paragraph_xml(text: str) -> str:
    return f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p>"


def _table_xml(table_rows: list[list[str]]) -> str:
    rows = []
    for row in table_rows:
        cells = "".join(f"<w:tc>{_paragraph_xml(cell)}</w:tc>" for cell in row)
        rows.append(f"<w:tr>{cells}</w:tr>")
    return f"<w:tbl>{''.join(rows)}</w:tbl>"


def _make_docx(
    paragraphs: list[str],
    table_rows: list[list[str]] | None = None,
    table_after_paragraph: int | None = None,
) -> bytes:
    body_parts: list[str] = []
    insert_after = len(paragraphs) - 1 if table_after_paragraph is None else table_after_paragraph
    for index, paragraph in enumerate(paragraphs):
        body_parts.append(_paragraph_xml(paragraph))
        if table_rows and index == insert_after:
            body_parts.append(_table_xml(table_rows))
    body = "".join(body_parts)
    if table_rows and insert_after < 0:
        body = _table_xml(table_rows) + body
    xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{body}</w:body>"
        "</w:document>"
    )
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


def test_docx_to_text_extracts_paragraphs() -> None:
    content = _make_docx(["中华人民共和国个人信息保护法", "第一条 为了保护个人信息权益。"])

    text = _docx_to_text(content)

    assert text == "中华人民共和国个人信息保护法\n第一条 为了保护个人信息权益。"


def test_docx_to_text_preserves_tables_in_body_order() -> None:
    content = _make_docx(
        ["第一条 表格前正文。", "第二条 表格后正文。"],
        table_rows=[["处理活动", "合规要求"], ["敏感个人信息", "单独同意"]],
        table_after_paragraph=0,
    )

    text = _docx_to_text(content)

    assert "第一条 表格前正文。" in text
    assert "<table>" in text
    assert "<td>敏感个人信息</td>" in text
    assert text.index("第一条") < text.index("<table>") < text.index("第二条")


def _source_record(source_id: str = "upload_001", file_format: str = "pdf") -> SourceRecord:
    return SourceRecord(
        source_id=source_id,
        title="用户上传合同",
        source_url="file:///upload.pdf",
        source_site="user_upload",
        doc_type="contract",
        file_format=file_format,
        include_in_mvp=True,
    )


def test_auto_parser_routes_pdf_to_docling(tmp_path: Path, monkeypatch) -> None:
    raw_path = tmp_path / "upload_001.pdf"
    raw_path.write_bytes(b"%PDF-1.4")

    def fake_docling(path: Path) -> ParsedText:
        assert path == raw_path
        return ParsedText("第一条 合同目的。", "docling_parser", "test")

    monkeypatch.setattr(normalize_module, "_docling_to_text", fake_docling)

    document = normalize_source(_source_record(), raw_path)

    assert document.text == "第一条 合同目的。"
    assert document.ingest_meta.parser == "docling_parser"


def test_mineru_parser_runs_cli_and_reads_markdown(tmp_path: Path, monkeypatch) -> None:
    raw_path = tmp_path / "upload_001.pdf"
    raw_path.write_bytes(b"%PDF-1.4")
    output_dir = tmp_path / "mineru"

    def fake_run(command, **kwargs):
        assert command[:2] == ["mineru.exe", "-p"]
        assert command[command.index("-b") + 1] == "pipeline"
        mineru_output = Path(command[command.index("-o") + 1])
        markdown_dir = mineru_output / "upload_001" / "auto"
        markdown_dir.mkdir(parents=True)
        (markdown_dir / "upload_001.md").write_text("## OCR 后正文\n合规义务。", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, stdout="ok", stderr="")

    monkeypatch.setattr(normalize_module.shutil, "which", lambda name: "mineru.exe")
    monkeypatch.setattr(normalize_module.subprocess, "run", fake_run)

    document = normalize_source(
        _source_record(),
        raw_path,
        parser="mineru",
        parser_output_dir=output_dir,
    )

    assert "OCR 后正文" in document.text
    assert document.ingest_meta.parser == "mineru_parser"


def test_docling_ocr_options_default_to_rapidocr(monkeypatch) -> None:
    pytest.importorskip("docling")
    monkeypatch.delenv("LAWAGENT_DOCLING_OCR_ENGINE", raising=False)

    options = normalize_module._docling_ocr_options()

    assert options.kind == "rapidocr"
    assert options.backend == "onnxruntime"
    assert options.lang == ["chinese", "english"]


def test_docling_ocr_options_can_use_remote_kserve(monkeypatch) -> None:
    pytest.importorskip("docling")
    monkeypatch.setenv("LAWAGENT_DOCLING_OCR_ENGINE", "kserve_v2_ocr")
    monkeypatch.setenv("LAWAGENT_DOCLING_OCR_API_URL", "http://127.0.0.1:8000")
    monkeypatch.setenv("LAWAGENT_DOCLING_OCR_MODEL_NAME", "paddleocr")
    monkeypatch.setenv("LAWAGENT_DOCLING_OCR_HEADERS", '{"Authorization": "Bearer test"}')

    options = normalize_module._docling_ocr_options()

    assert options.kind == "kserve_v2_ocr"
    assert options.url == "http://127.0.0.1:8000"
    assert options.model_name == "paddleocr"
    assert options.transport == "http"
    assert options.headers == {"Authorization": "Bearer test"}


def test_default_docling_artifacts_ignores_incomplete_rapidocr_dir(
    tmp_path: Path, monkeypatch
) -> None:
    artifacts = tmp_path / "docling"
    artifacts.mkdir()
    monkeypatch.setattr(normalize_module, "DEFAULT_DOCLING_ARTIFACTS_PATH", artifacts)
    monkeypatch.delenv("LAWAGENT_DOCLING_ARTIFACTS_PATH", raising=False)

    assert normalize_module._docling_artifacts_path(ocr_engine="rapidocr") is None
