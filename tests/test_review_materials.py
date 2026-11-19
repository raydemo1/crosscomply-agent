import json
from pathlib import Path

from openpyxl import Workbook

from law_agent.data.schemas import Document, IngestMeta, SourceRecord
from law_agent.review import materials as materials_module
from law_agent.review.material_search import (
    INLINE_BYTES,
    READ_BYTES,
    MaterialFile,
    MaterialReadRequest,
    grep_materials,
    inline_material_passages,
    material_inventory,
    read_materials,
)
from law_agent.review.materials import material_from_file, material_from_text


def test_material_from_text_marks_pasted_text() -> None:
    material = material_from_text("手机号发送给新加坡服务商。")

    assert material.input_mode == "pasted_text"
    assert material.material_text == "手机号发送给新加坡服务商。"
    assert material.parser == "pasted_text"
    assert material.uploaded_file is None


def test_material_from_file_reads_utf8_text(tmp_path: Path) -> None:
    path = tmp_path / "scenario.txt"
    path.write_text("手机号发送给新加坡服务商。", encoding="utf-8")

    material = material_from_file(path)

    assert material.input_mode == "uploaded_file"
    assert material.material_text == "手机号发送给新加坡服务商。"
    assert material.source_name == "scenario.txt"
    assert material.parser == "plain_text_parser"
    assert material.uploaded_file is not None
    assert material.uploaded_file.filename == "scenario.txt"


def test_material_from_file_routes_pdf_through_normalize(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "upload.pdf"
    path.write_bytes(b"%PDF-1.4")

    def fake_normalize_source(
        record: SourceRecord,
        raw_path: Path,
        *,
        parser="auto",
        parser_output_dir=None,
    ) -> Document:
        assert raw_path == path
        assert parser == "auto"
        assert record.source_site == "user_upload"
        return Document(
            doc_id=record.source_id,
            source_id=record.source_id,
            title=record.title,
            source_url=record.source_url,
            source_site=record.source_site,
            doc_type=record.doc_type,
            raw_format="pdf",
            text="解析后的 PDF 正文",
            ingest_meta=IngestMeta(
                fetched_at="2026-07-06T00:00:00+00:00",
                parser="docling_parser",
                parser_version="test",
            ),
        )

    monkeypatch.setattr(materials_module, "normalize_source", fake_normalize_source)

    material = material_from_file(path)

    assert material.material_text == "解析后的 PDF 正文"
    assert material.parser == "docling_parser"
    assert material.parser_version == "test"
    assert material.uploaded_file is not None
    assert material.uploaded_file.raw_format == "pdf"


def test_full_batch_reads_each_file_without_reading_other_versions():
    files = [MaterialFile("a", "甲.pdf", "甲合同。"), MaterialFile("b", "乙.docx", "乙合同。")]
    result = read_materials(
        files,
        [
            MaterialReadRequest(material_version_id="a"),
            MaterialReadRequest(material_version_id="other"),
            MaterialReadRequest(material_version_id="b"),
        ],
    )["passages"]
    assert [item.get("text") for item in result] == ["甲合同。", None, "乙合同。"]
    assert result[0]["next_read"] is None and result[2]["next_read"] is None
    assert "error" in result[1]


def test_batch_budget_and_resumption_use_file_local_character_offsets():
    text = "数据😀" * READ_BYTES
    files = [MaterialFile("a", "甲.txt", text), MaterialFile("b", "乙.txt", text)]
    requests = [MaterialReadRequest(material_version_id=f.id, start=1, end=20000) for f in files]
    results = read_materials(files, requests)["passages"]
    assert sum(len(item["text"].encode("utf-8")) for item in results) <= READ_BYTES
    for request, result in zip(requests, results, strict=True):
        assert result["truncated"] and result["text"] == text[1 : result["end"]]
        following = read_materials(files, [MaterialReadRequest(**result["next_read"])])["passages"][
            0
        ]
        assert following["offset"] == result["end"]
        assert following["text"] == text[result["end"] : following["end"]]
        assert following["next_read"] is None or following["next_read"]["end"] == request.end


def test_inline_budget_and_literal_grep_preserve_exact_source():
    files = [
        MaterialFile("a", "短文本.txt", "已确认人数。", inline=True),
        MaterialFile("b", "长文本.txt", "乙" * INLINE_BYTES, inline=True),
        MaterialFile("c", "协议.pdf", "数据用途" + "甲" * 40 + "[条款]" + "乙" * 40 + "[条款]"),
    ]
    assert [passage["material_version_id"] for passage in inline_material_passages(files)] == ["a"]
    result = grep_materials(files, "[条款]", ["c"])
    assert len(result["hits"]) == 1
    hit = result["hits"][0]
    assert hit["text"] == files[2].text[hit["offset"] : hit["offset"] + len(hit["text"])]
    assert files[2].text[hit["match_start"] : hit["match_end"]] == "[条款]"


def test_xlsx_sheet_reads_keep_rows_and_offsets(tmp_path):
    path = tmp_path / "人数.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "主台账"
    sheet.append(["编号", "人数"])
    sheet.append(["001", 42])
    sheet = workbook.create_sheet('撤销"记录')
    sheet.append(["编号", "原因"])
    sheet.append(["002", "第一行\n第二行"])
    workbook.save(path)
    workbook.close()
    parsed = material_from_file(path)
    file = MaterialFile("sheet", path.name, parsed.material_text)
    assert material_inventory([file])[0]["sheets"] == ["主台账", '撤销"记录']
    result = read_materials(
        [file],
        [
            MaterialReadRequest(material_version_id=file.id, sheet='撤销"记录'),
        ],
    )["passages"][0]
    assert result["offset"] > 0 and result["text"] == file.text[result["offset"] : result["end"]]
    assert [json.loads(line) for line in result["text"].splitlines()[1:]] == [
        ["编号", "原因"],
        ["002", "第一行\n第二行"],
    ]
