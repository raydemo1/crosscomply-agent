from law_agent.data.chunking.law import (
    chunk_law_document,
    has_ordered_articles,
    split_law_article_sections,
    split_law_articles,
)
from law_agent.data.schemas import Document, IngestMeta


def test_split_law_articles_preserves_article_markers() -> None:
    text = "第一条  保护个人信息。\n第二条  规范处理活动。"

    articles = split_law_articles(text)

    assert articles == [
        ("第一条", "第一条  保护个人信息。"),
        ("第二条", "第二条  规范处理活动。"),
    ]


def test_split_law_articles_accepts_docling_markdown_article_headings() -> None:
    """Docling may emit each PDF article as a Markdown heading."""

    text = "## 第一条\n保护个人信息。\n\n## 第二条\n规范处理活动。"

    assert split_law_articles(text) == [
        ("第一条", "第一条\n保护个人信息。"),
        ("第二条", "第二条\n规范处理活动。"),
    ]


def test_chunk_law_document_keeps_chapter_and_section_path() -> None:
    document = Document(
        doc_id="flk_npc_ff8081817b6472a3017b656cc2040044",
        source_id="flk_npc_ff8081817b6472a3017b656cc2040044",
        title="中华人民共和国个人信息保护法",
        source_url="https://flk.npc.gov.cn/",
        source_site="flk.npc.gov.cn",
        doc_type="law",
        authority="national_law",
        citation_role="primary_legal_basis",
        law_status="effective",
        topic_tags=["个人信息保护"],
        text=(
            "第一章 总则\n"
            "第一条  保护个人信息。\n"
            "第二章 个人信息处理规则\n"
            "第一节 一般规定\n"
            "第13条  处理个人信息应当取得同意。"
        ),
        ingest_meta=IngestMeta(
            fetched_at="2026-07-01T00:00:00Z",
            parser="test_parser",
            parser_version="0.1.0",
        ),
    )

    chunks = chunk_law_document(document)

    assert chunks[0].heading_path == ["中华人民共和国个人信息保护法", "第一章 总则", "第一条"]
    assert chunks[1].article_no == "第13条"
    assert chunks[1].heading_path == [
        "中华人民共和国个人信息保护法",
        "第二章 个人信息处理规则",
        "第一节 一般规定",
        "第13条",
    ]
    assert chunks[1].citation_label == "中华人民共和国个人信息保护法 第13条"
    assert chunks[1].citation_role == "primary_legal_basis"
    assert chunks[1].can_cite_clause is True


def test_chunk_law_document_keeps_article_chunk_when_article_is_not_oversized() -> None:
    document = Document(
        doc_id="flk_npc_civil_code",
        source_id="flk_npc_civil_code",
        title="中华人民共和国民法典",
        source_url="https://flk.npc.gov.cn/",
        source_site="flk.npc.gov.cn",
        doc_type="law",
        authority="national_law",
        law_status="effective",
        topic_tags=["合同"],
        legal_domain=["民事", "合同"],
        text=(
            "第三编 合同\n"
            "第三章 合同的效力\n"
            "第五百零九条 当事人应当按照约定全面履行自己的义务。\n"
            "当事人应当遵循诚信原则，根据合同的性质、目的和交易习惯履行通知、协助、保密等义务。\n"
            "当事人在履行合同过程中，应当避免浪费资源、污染环境和破坏生态。\n"
            "第五百一十条 合同生效后，当事人就质量、价款或者报酬、履行地点等内容没有约定或者约定不明确的，可以协议补充；\n"
            "（一）不能达成补充协议的，按照合同相关条款或者交易习惯确定；\n"
            "（二）仍不能确定的，适用本法其他规定。"
        ),
        ingest_meta=IngestMeta(
            fetched_at="2026-07-01T00:00:00Z",
            parser="test_parser",
            parser_version="0.1.0",
        ),
    )

    chunks = chunk_law_document(document)

    assert len(chunks) == 2
    assert chunks[0].paragraph_no is None
    assert chunks[0].item_no is None
    assert chunks[1].heading_path == [
        "中华人民共和国民法典",
        "第三编 合同",
        "第三章 合同的效力",
        "第五百一十条",
    ]
    assert chunks[1].citation_label == "中华人民共和国民法典 第五百一十条"
    assert "（一）不能达成补充协议" in chunks[1].text
    assert chunks[1].legal_domain == ["民事", "合同"]


def test_chunk_law_document_does_not_split_on_inline_article_references() -> None:
    document = Document(
        doc_id="flk_npc_pipl_2021",
        source_id="flk_npc_pipl_2021",
        title="中华人民共和国个人信息保护法",
        source_url="https://flk.npc.gov.cn/",
        source_site="flk.npc.gov.cn",
        doc_type="law",
        authority="national_law",
        law_status="effective",
        topic_tags=["个人信息保护"],
        text=(
            "第三章 个人信息跨境提供的规则\n"
            "第三十八条 个人信息处理者向境外提供个人信息，应当具备下列条件之一：\n"
            "（一）依照本法第四十条的规定通过国家网信部门组织的安全评估；\n"
            "（二）按照国家网信部门的规定经专业机构进行个人信息保护认证；\n"
            "第四十条 关键信息基础设施运营者应当将在境内收集和产生的个人信息存储在境内。"
        ),
        ingest_meta=IngestMeta(
            fetched_at="2026-07-01T00:00:00Z",
            parser="test_parser",
            parser_version="0.1.0",
        ),
    )

    chunks = chunk_law_document(document)

    assert [chunk.article_no for chunk in chunks] == ["第三十八条", "第四十条"]
    assert "依照本法第四十条" in chunks[0].text
    assert chunks[0].citation_label == "中华人民共和国个人信息保护法 第三十八条"


def test_chunk_law_document_splits_oversized_article_to_paragraphs_not_items() -> None:
    long_paragraph = "当事人应当按照约定全面履行自己的义务。" * 45
    item_text = "（一）不能达成补充协议的，按照合同相关条款或者交易习惯确定；"
    document = Document(
        doc_id="flk_npc_civil_code",
        source_id="flk_npc_civil_code",
        title="中华人民共和国民法典",
        source_url="https://flk.npc.gov.cn/",
        source_site="flk.npc.gov.cn",
        doc_type="law",
        authority="national_law",
        law_status="effective",
        topic_tags=["合同"],
        legal_domain=["民事", "合同"],
        text=(
            "第三编 合同\n"
            "第三章 合同的效力\n"
            f"第五百零九条 {long_paragraph}\n"
            f"{long_paragraph}\n"
            f"{item_text}\n"
            "（二）仍不能确定的，适用本法其他规定。"
        ),
        ingest_meta=IngestMeta(
            fetched_at="2026-07-01T00:00:00Z",
            parser="test_parser",
            parser_version="0.1.0",
        ),
    )

    chunks = chunk_law_document(document)

    assert [chunk.paragraph_no for chunk in chunks] == ["第1款", "第2款"]
    assert all(chunk.item_no is None for chunk in chunks)
    assert chunks[1].heading_path == [
        "中华人民共和国民法典",
        "第三编 合同",
        "第三章 合同的效力",
        "第五百零九条",
        "第2款",
    ]
    assert chunks[1].citation_label == "中华人民共和国民法典 第五百零九条 第2款"
    assert item_text in chunks[1].text


def test_chunk_law_document_keeps_parent_traceability() -> None:
    document = Document(
        doc_id="flk_npc_pipl_2021",
        source_id="flk_npc_pipl_2021",
        title="中华人民共和国个人信息保护法",
        source_url="https://flk.npc.gov.cn/",
        source_site="flk.npc.gov.cn",
        doc_type="law",
        authority="national_law",
        law_status="effective",
        topic_tags=["个人信息保护"],
        text="第一条  保护个人信息。\n第二条  规范处理活动。",
        ingest_meta=IngestMeta(
            fetched_at="2026-07-01T00:00:00Z",
            parser="test_parser",
            parser_version="0.1.0",
        ),
    )

    chunks = chunk_law_document(document)

    assert [chunk.article_no for chunk in chunks] == ["第一条", "第二条"]
    assert chunks[0].next_chunk_id == "flk_npc_pipl_2021:0001"
    assert chunks[1].prev_chunk_id == "flk_npc_pipl_2021:0000"
    assert chunks[0].heading_path == ["中华人民共和国个人信息保护法", "第一条"]
    assert chunks[0].authority == "national_law"


def test_chunk_law_document_marks_auxiliary_sources_not_clause_citable() -> None:
    document = Document(
        doc_id="cac_data_export_assessment_qna_2022",
        source_id="cac_data_export_assessment_qna_2022",
        title="《数据出境安全评估办法》答记者问",
        source_url="https://www.cac.gov.cn/",
        source_site="cac.gov.cn",
        doc_type="policy",
        authority="public_interpretation",
        law_status="effective",
        text="第一条 这只是问答材料中的编号，不应作为具体条款引用。",
        ingest_meta=IngestMeta(
            fetched_at="2026-07-01T00:00:00Z",
            parser="test_parser",
            parser_version="0.1.0",
        ),
    )

    chunks = chunk_law_document(document)

    assert chunks[0].citation_role == "interpretation_auxiliary"
    assert chunks[0].can_cite_clause is False


def test_has_ordered_articles_rejects_articles_quoted_inside_a_list() -> None:
    """A negative list cites articles; it is not built out of them."""

    body = (
        "行业领域一：地理信息与气象数据服务\n"
        "属于《促进和规范数据跨境流动规定》第三条、第六条规定情形的，不计入累计数量。\n"
        "第三条规定的数据出境活动，由数据处理者自行判断。\n"
        "第六条规定的数据出境活动，不计入累计数量。\n"
        "第九条规定的数据出境活动，不计入累计数量。\n"
    )

    assert has_ordered_articles(body) is False


def test_has_ordered_articles_accepts_a_real_instrument_split_by_blank_lines() -> None:
    body = (
        "第一条 为了规范数据出境活动，制定本办法。\n\n"
        "第二条 数据处理者向境外提供数据，适用本办法。\n\n"
        "第三条 数据出境安全评估坚持事前评估和持续监督相结合。\n"
    )

    assert has_ordered_articles(body) is True


def test_has_ordered_articles_accepts_articles_written_over_several_lines() -> None:
    """A real instrument writes each article over as many lines as it needs."""

    body = (
        "第一条 定义\n"
        "在本合同中，除上下文另有规定外：\n"
        "（一）个人信息处理者，是指自主决定处理目的的组织。\n"
        "第二条 个人信息处理者的义务和责任\n"
        "个人信息处理者应当履行下列义务和责任：\n"
        "（一）按照属地相关法律法规及本合同要求处理个人信息。\n"
        "第三条 个人信息的处理\n"
        "个人信息处理者应当遵循合法、正当、必要原则。\n"
    )

    assert has_ordered_articles(body) is True


def test_split_law_article_sections_keeps_preamble_before_the_first_article() -> None:
    text = (
        "本清单说明\n"
        "为促进数据跨境安全有序流动，制定本清单，自发布之日起施行。\n"
        "第一章 总则\n"
        "第一条 本清单适用于自由贸易试验区。\n"
        "第二条 本清单由省级网信部门负责解释。"
    )

    sections = split_law_article_sections(text)

    assert [section.article_no for section in sections] == ["", "第一条", "第二条"]
    assert "本清单说明" in sections[0].text
    assert "自发布之日起施行" in sections[0].text


def test_split_law_articles_keeps_cross_reference_inside_current_clause() -> None:
    sections = split_law_article_sections(
        "第五条 个人信息主体的权利。\n"
        "第九条第五项。\n"
        "上述约定不影响个人信息主体的法定权利。\n"
        "第六条 救济。"
    )

    assert [section.article_no for section in sections] == ["第五条", "第六条"]
    assert "第九条第五项" in sections[0].text


def test_chunk_law_document_publishes_preamble_without_inventing_an_article_no() -> None:
    document = Document(
        doc_id="guangxi_free_trade_zone_data_export_negative_list_2025",
        source_id="guangxi_free_trade_zone_data_export_negative_list_2025",
        title="中国（广西）自由贸易试验区数据出境管理清单（负面清单）",
        source_url="https://example.test/",
        source_site="example.test",
        doc_type="regulation",
        authority="local_regulation",
        law_status="effective",
        text=(
            "本清单说明\n"
            "为促进数据跨境安全有序流动，制定本清单，自发布之日起施行。\n"
            "第一条 本清单适用于自由贸易试验区。\n"
            "第二条 本清单由省级网信部门负责解释。"
        ),
        ingest_meta=IngestMeta(
            fetched_at="2026-07-01T00:00:00Z",
            parser="test_parser",
            parser_version="0.1.0",
        ),
    )

    chunks = chunk_law_document(document)

    assert chunks[0].article_no is None
    assert "本清单说明" in chunks[0].text
    assert "自发布之日起施行" in chunks[0].text
    assert [chunk.article_no for chunk in chunks[1:]] == ["第一条", "第二条"]
