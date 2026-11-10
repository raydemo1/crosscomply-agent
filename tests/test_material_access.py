import json

from openpyxl import Workbook

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
from law_agent.review.materials import material_from_file


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
