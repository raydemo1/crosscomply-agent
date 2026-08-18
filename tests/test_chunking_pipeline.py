from law_agent.data.chunking.pipeline import chunk_document
from law_agent.data.schemas import Document, IngestMeta


def _document(**overrides: object) -> Document:
    data = {
        "doc_id": "doc",
        "source_id": "doc",
        "title": "测试文件",
        "source_url": "https://example.test/",
        "source_site": "example.test",
        "doc_type": "guideline",
        "authority": "public_interpretation",
        "law_status": "effective",
        "text": "正文",
        "ingest_meta": IngestMeta(
            fetched_at="2026-07-01T00:00:00Z",
            parser="test_parser",
            parser_version="0.1.0",
        ),
    }
    data.update(overrides)
    return Document.model_validate(data)


def test_chunk_document_splits_faq_by_question_blocks() -> None:
    document = _document(
        doc_type="faq",
        title="数据出境安全管理政策问答",
        text=(
            "问：什么情形需要申报数据出境安全评估？\n"
            "答：达到规定数量或者属于重要数据的，应当申报。\n"
            "问：标准合同备案如何办理？\n"
            "答：按照所在地省级网信部门要求提交材料。"
        ),
    )

    chunks = chunk_document(document)

    assert len(chunks) == 2
    assert chunks[0].heading_path == [
        "数据出境安全管理政策问答",
        "问：什么情形需要申报数据出境安全评估？",
    ]
    assert (
        chunks[0].citation_label
        == "数据出境安全管理政策问答 问：什么情形需要申报数据出境安全评估？"
    )
    assert "答：达到规定数量" in chunks[0].text
    assert chunks[0].can_cite_clause is False


def test_chunk_document_splits_guideline_by_markdown_and_numeric_headings() -> None:
    document = _document(
        title="网络数据分类分级指引",
        text=(
            "## 1 范围\n"
            "本文件规定了网络数据分类分级方法。\n"
            "## 2 术语和定义\n"
            "下列术语适用于本文件。\n"
            "2.1 重要数据\n"
            "一旦遭到篡改、破坏、泄露可能危害国家安全的数据。"
        ),
    )

    chunks = chunk_document(document)

    assert [chunk.heading_path[-1] for chunk in chunks] == [
        "1 范围",
        "2 术语和定义",
    ]
    assert "2.1 重要数据" in chunks[1].text
    assert "一旦遭到篡改、破坏、泄露可能危害国家安全的数据。" in chunks[1].text
    assert chunks[1].citation_label == "网络数据分类分级指引 2 术语和定义"


def test_chunk_document_drops_heading_only_chunks() -> None:
    document = _document(
        title="数据安全技术 敏感个人信息处理安全要求",
        text=(
            "## 3\n"
            "## 3.1\n"
            "个人信息\n"
            "以电子或者其他方式记录的与已识别或者可识别的自然人有关的各种信息。\n"
            "## 3.2\n"
            "敏感个人信息\n"
            "一旦泄露或者非法使用，容易导致自然人人身财产安全受到危害的个人信息。"
        ),
    )

    chunks = chunk_document(document)

    assert all(chunk.text not in {"3", "3.1", "3.2"} for chunk in chunks)
    assert any("以电子或者其他方式记录" in chunk.text for chunk in chunks)
    assert any("一旦泄露或者非法使用" in chunk.text for chunk in chunks)


def test_chunk_document_merges_tiny_prefix_with_following_chunk() -> None:
    document = _document(
        title="个人信息出境标准合同（范本）",
        text=(
            "附件：\n"
            "## 个人信息出境标准合同\n"
            "为了确保境外接收方处理个人信息的活动达到中华人民共和国相关法律法规规定的个人信息保护标准，"
            "明确个人信息处理者和境外接收方个人信息保护的权利和义务。"
        ),
    )

    chunks = chunk_document(document)

    assert chunks[0].char_count >= 20
    assert chunks[0].text.startswith("附件：\n个人信息出境标准合同")
    assert all(chunk.char_count >= 20 for chunk in chunks)


def test_chunk_document_splits_negative_list_table_rows_under_limit() -> None:
    row = (
        "<tr><td>个人信息</td><td>自当年1月1日起累计向境外提供100万人以上个人信息。</td>"
        "<td>仅限于会员管理场景，个人信息计算数量以自然人为单位去重。</td></tr>"
    )
    document = _document(
        source_id="guangxi_free_trade_zone_data_export_negative_list_2025",
        doc_id="guangxi_free_trade_zone_data_export_negative_list_2025",
        title="中国（广西）自由贸易试验区数据出境管理清单（负面清单）（2025版）",
        text="行业领域一：地理信息与气象数据服务\n<table>" + row * 20 + "</table>",
    )

    chunks = chunk_document(document)

    assert len(chunks) > 1
    assert all(chunk.char_count <= 1200 for chunk in chunks)
    table_chunk = next(chunk for chunk in chunks if chunk.heading_path[-1] == "表格1")
    assert table_chunk.heading_path == [
        "中国（广西）自由贸易试验区数据出境管理清单（负面清单）（2025版）",
        "行业领域一：地理信息与气象数据服务",
        "表格1",
    ]
    assert "个人信息" in table_chunk.text
    assert table_chunk.citation_role == "conditional_local_basis"


def test_chunk_document_detects_bold_markdown_law_articles_in_policy() -> None:
    document = _document(
        doc_id="cac_data_export_security_assessment_measures_2022",
        source_id="cac_data_export_security_assessment_measures_2022",
        title="数据出境安全评估办法",
        doc_type="policy",
        authority="ministry_policy",
        text=(
            "**第一条** 为了规范数据出境活动，制定本办法。\n\n"
            "**第二条** 数据处理者向境外提供数据，适用本办法。\n\n"
            "**第三条** 数据出境安全评估坚持事前评估和持续监督相结合。\n\n"
            "**第四条** 数据处理者向境外提供数据，有下列情形之一的，"
            "应当通过所在地省级网信部门向国家网信部门申报数据出境安全评估：\n\n"
            "（一）数据处理者向境外提供重要数据；\n\n"
            "（二）关键信息基础设施运营者和处理100万人以上个人信息的数据处理者"
            "向境外提供个人信息；\n\n"
            "（三）自上年1月1日起累计向境外提供10万人个人信息或者1万人敏感"
            "个人信息的数据处理者向境外提供个人信息；\n\n"
            "（四）国家网信部门规定的其他需要申报数据出境安全评估的情形。\n\n"
            "**第五条** 数据处理者在申报数据出境安全评估前，应当开展自评估。"
        ),
    )

    chunks = chunk_document(document)

    fourth = next(chunk for chunk in chunks if chunk.article_no == "第四条")
    assert fourth.heading_path == ["数据出境安全评估办法", "第四条"]
    assert "申报数据出境安全评估" in fourth.text
    assert "（一）数据处理者向境外提供重要数据" in fourth.text
    assert "（四）国家网信部门规定的其他需要申报" in fourth.text
    assert "**第五条**" not in fourth.text
