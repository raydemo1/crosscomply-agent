import subprocess
import zipfile
from io import BytesIO
from pathlib import Path

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

    def fake_docling(path: Path, *, ocr: bool, strategy: str | None = None) -> ParsedText:
        assert path == raw_path
        # No readable text layer in ``b"%PDF-1.4"``, so OCR is the only read.
        assert ocr is True
        return ParsedText("第一条 合同目的。", "docling_parser", "test", ocr_used=ocr)

    monkeypatch.setattr(normalize_module, "_docling_to_text", fake_docling)

    document = normalize_source(_source_record(), raw_path)

    assert document.text == "第一条 合同目的。"
    assert document.ingest_meta.parser == "docling_parser"
    assert document.ingest_meta.ocr_used is True


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


def test_default_docling_artifacts_ignores_incomplete_rapidocr_dir(
    tmp_path: Path, monkeypatch
) -> None:
    artifacts = tmp_path / "docling"
    artifacts.mkdir()
    monkeypatch.setattr(normalize_module, "DEFAULT_DOCLING_ARTIFACTS_PATH", artifacts)
    monkeypatch.delenv("LAWAGENT_DOCLING_ARTIFACTS_PATH", raising=False)

    assert normalize_module._docling_artifacts_path(ocr_engine="rapidocr") is None


def test_tableformer_requires_complete_v2_artifacts(tmp_path: Path) -> None:
    model_dir = tmp_path / "docling-project--TableFormerV2"
    model_dir.mkdir()
    for name in ("model.safetensors", "config.json"):
        (model_dir / name).touch()

    assert normalize_module._docling_tableformer_available(tmp_path) is False

    (model_dir / "tokenizer.json").touch()

    assert normalize_module._docling_tableformer_available(tmp_path) is True


def test_tableformer_rejects_v1_artifacts(tmp_path: Path) -> None:
    model_dir = (
        tmp_path / "docling-project--docling-models" / "model_artifacts" / "tableformer" / "fast"
    )
    model_dir.mkdir(parents=True)
    for name in ("tm_config.json", "tableformer_fast.safetensors"):
        (model_dir / name).touch()

    assert normalize_module._docling_tableformer_available(tmp_path) is False


def test_vertically_merged_cells_repeat_the_text_they_continue() -> None:
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    restart = '<w:tcPr><w:vMerge w:val="restart"/></w:tcPr>'
    cont = "<w:tcPr><w:vMerge/></w:tcPr>"
    rows = (
        f"<w:tr><w:tc>{restart}{_paragraph_xml('个人信息')}</w:tc><w:tc>{_paragraph_xml('第一项')}</w:tc></w:tr>"
        f"<w:tr><w:tc>{cont}<w:p/></w:tc><w:tc>{_paragraph_xml('第二项')}</w:tc></w:tr>"
        f"<w:tr><w:tc>{_paragraph_xml('重要数据')}</w:tc><w:tc>{_paragraph_xml('第三项')}</w:tc></w:tr>"
    )
    xml = f'<?xml version="1.0" encoding="UTF-8"?><w:document {w}><w:body><w:tbl>{rows}</w:tbl></w:body></w:document>'
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)

    text = _docx_to_text(buffer.getvalue())

    assert "<tr><td>个人信息</td><td>第二项</td></tr>" in text
    assert "<tr><td>重要数据</td><td>第三项</td></tr>" in text


def test_an_html_page_keeps_its_article_and_drops_the_site_around_it(tmp_path) -> None:
    page = (
        '<html><head><meta charset="gbk"><title>测试规定_中国政府网</title></head><body>'
        '<div class="header"><a>首页</a>|<a>简</a>|<a>繁</a><a>登录</a></div>'
        '<div class="crumb">首页 > 政策 > 测试</div>'
        '<div id="UCAP-CONTENT"><h1>测试规定</h1><p>第一条 为了规范数据处理活动，保障数据安全，制定本规定。</p>'
        '<p>第二条 本规定适用于在中华人民共和国境内开展的数据处理活动及其安全监管。</p>'
        '<table><tr><td>类别</td><td>要求</td></tr><tr><td>重要数据</td><td>出境前申报安全评估</td></tr></table></div>'
        '<div class="footer">主办单位：国务院办公厅 版权所有：中国政府网</div></body></html>'
    )
    path = tmp_path / "page.html"
    path.write_bytes(page.encode("gb18030"))
    source = SourceRecord(source_id="page", title="测试规定", source_url="https://www.gov.cn/x.htm",
                          source_site="gov.cn", doc_type="regulation", file_format="html")

    text = normalize_source(source, path).text

    assert text.startswith("测试规定\n\n第一条 为了规范数据处理活动")
    assert "<table><tr><td>类别</td><td>要求</td></tr><tr><td>重要数据</td><td>出境前申报安全评估</td></tr></table>" in text
    assert not any(word in text for word in ("登录", "首页", "主办单位", "版权所有"))
