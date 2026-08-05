from law_agent.data.cleaners.common import clean_text


def test_clean_text_removes_mechanical_noise_without_rewriting_articles() -> None:
    raw = (
        "中华人民共和国个人信息保护法\r\n"
        "中华人民共和国个人信息保护法\r\n"
        "\u0000第一条  为了保护个人信息权益，规范个人信息处理活动。\r\n"
        "\r\n"
        "\r\n"
        "第二条  自然人的个人信息受法律保护。   \r\n"
    )

    result = clean_text(raw, title="中华人民共和国个人信息保护法")

    assert result.text.count("中华人民共和国个人信息保护法") == 1
    assert "第一条  为了保护个人信息权益，规范个人信息处理活动。" in result.text
    assert "第二条  自然人的个人信息受法律保护。" in result.text
    assert result.rule_hits["duplicate_title"] == 1
    assert result.rule_hits["control_chars"] == 1


def test_clean_text_removes_repeated_contents_table() -> None:
    raw = (
        "中华人民共和国个人信息保护法\n"
        "目　　录\n"
        "第一章　总　　则\n"
        "第二章　个人信息处理规则\n"
        "第一章　总　　则\n"
        "第一条　为了保护个人信息权益，规范个人信息处理活动。\n"
    )

    result = clean_text(raw, title="中华人民共和国个人信息保护法")

    assert "目　　录" not in result.text
    assert result.text.count("第一章　总　　则") == 1
    assert "第一条　为了保护个人信息权益" in result.text
    assert result.rule_hits["contents_table_lines"] == 3


def test_clean_text_removes_docling_markdown_law_contents_table() -> None:
    """PDF parsers can serialize both the TOC and law body as Markdown headings."""

    raw = (
        "## 深圳经济特区数据条例\n"
        "## 目 录\n"
        "## 第一章 总则\n"
        "## 第二章 个人数据\n"
        "## 第一章 总则\n"
        "## 第一条 为了规范数据处理活动，保护自然人合法权益。\n"
        "## 第二条 本条例中下列用语的含义。\n"
    )

    result = clean_text(raw, title="深圳经济特区数据条例")

    assert "目 录" not in result.text
    assert result.text.count("第一章 总则") == 1
    assert "## 第一条 为了规范数据处理活动" in result.text
    assert "## 第二条 本条例中下列用语的含义" in result.text


def test_clean_text_removes_docling_standard_contents_markdown_table() -> None:
    """A PDF standard's contents table must not become retrievable text."""

    raw = (
        "## 信息安全技术 个人信息安全规范\n"
        "## 目 次\n"
        "| 前言 |\n"
        "|---|\n"
        "| 1 范围 |\n"
        "| 2 规范性引用文件 |\n"
        "## 前言\n"
        "本文件按照 GB/T 1.1—2020 给出的规则起草。\n"
        "## 1 范围\n"
        "本文件规定了个人信息处理活动应遵循的原则。\n"
    )

    result = clean_text(raw, title="信息安全技术 个人信息安全规范")

    assert "目 次" not in result.text
    assert "| 1 范围 |" not in result.text
    assert "## 前言" in result.text
    assert "## 1 范围" in result.text
    assert result.rule_hits["contents_table_lines"] == 5


def test_clean_text_removes_web_boilerplate_from_cac_pages() -> None:
    raw = (
        "Title: 国家互联网信息办公室发布《数据出境安全评估申报指南（第三版）》\n"
        "URL Source: https://www.cac.gov.cn/example.htm\n"
        "Markdown Content:\n"
        "2026年07月02日 星期四\n"
        "[设为首页](#)[加入收藏](#)[手机版](#)[繁體](#)\n"
        "*   ![Image 1](logo.png)\n"
        "当前位置：[首页](https://www.cac.gov.cn/)>[正文](javascript:void(0);)\n"
        "# 国家互联网信息办公室发布《数据出境安全评估申报指南（第三版）》\n"
        "为了指导和帮助数据处理者规范有序申报数据出境安全评估。\n"
        "关闭\n"
        "Produced By CMS 网站群内容管理系统 publishdate:2025/06/27 17:01:53\n"
    )

    result = clean_text(raw, title="数据出境安全评估申报指南（第三版）")

    assert "Title:" not in result.text
    assert "设为首页" not in result.text
    assert "Produced By CMS" not in result.text
    assert "为了指导和帮助数据处理者" in result.text
    assert result.rule_hits["web_boilerplate_lines"] == 9


def test_clean_text_removes_dot_leader_toc_but_keeps_front_matter_body() -> None:
    raw = (
        "目次\n"
        "前言 ................................................................................ III\n"
        "引言 ................................................................................. IV\n"
        "1 范围 ................................................................................ 1\n"
        "前言\n"
        "本文件按照 GB/T 1.1 的规定起草。\n"
        "引言\n"
        "本文件用于指导数据分类分级工作。\n"
    )

    result = clean_text(raw)

    assert "................................................................" not in result.text
    assert "前言\n本文件按照 GB/T 1.1 的规定起草。" in result.text
    assert "引言\n本文件用于指导数据分类分级工作。" in result.text
    assert result.rule_hits["dot_leader_toc_lines"] == 3


def test_clean_text_removes_standard_toc_block_with_table_dividers() -> None:
    """GB/T standard TOC: ## 目 次 heading + table rows + |--- dividers."""
    raw = (
        "## 中华人民共和国国家标准\n"
        "## 目 次\n"
        "| 前言  ..........................................................................\n"
        "|--------------------------------------------------------------------------\n"
        "| 9.2  个人信息共享、转让  .........................................................\n"
        "|--------------------------------------------------------------------------\n"
        "## 前 言\n"
        "本标准按照 GB/T 1.1 的规定起草。\n"
        "## 1 范围\n"
        "本标准规定了个人信息处理活动。\n"
    )

    result = clean_text(raw)

    assert "目 次" not in result.text
    assert "...................." not in result.text
    assert "|--------" not in result.text
    assert "本标准按照 GB/T 1.1 的规定起草。" in result.text
    assert "本标准规定了个人信息处理活动。" in result.text
    assert result.rule_hits["contents_table_lines"] >= 5


def test_clean_text_collapses_spaced_latin_pdf_artifact() -> None:
    """PDF character-spacing artifact 'D a t a' should collapse to 'Data'."""
    raw = (
        "## 数据安全技术 敏感个人信息处理安全要求\n"
        "D a t a s e c u r i t y t e c h n o l o g y\n"
        "正文内容保持不变。\n"
    )

    result = clean_text(raw)

    assert "Datas e c u r i t y" not in result.text
    assert "Datasecurity" in result.text or "Datas" in result.text
    assert "正文内容保持不变。" in result.text
    assert result.rule_hits["spaced_latin"] >= 1


def test_clean_text_merges_isolated_clause_number_with_heading() -> None:
    """Bare '3.2' on its own line should merge with the following heading."""
    raw = (
        "3.1\n"
        "个人信息 personal information\n"
        "以电子或者其他方式记录的能够识别特定自然人的信息。\n"
        "3.2\n"
        "## 个人敏感信息\n"
        "一旦泄露可能危害自然人安全的个人信息。\n"
    )

    result = clean_text(raw)

    # Isolated numbers merged into headings, not standalone lines.
    assert "\n3.1\n" not in result.text
    assert "\n3.2\n" not in result.text
    # Merged form present (number + title on one line).
    assert "3.1 个人信息" in result.text
    assert "3.2 个人敏感信息" in result.text
    assert result.rule_hits["merged_isolated_numbers"] == 2


def test_clean_text_removes_pdf_parser_mechanical_lines() -> None:
    raw = (
        "Number of Pages: 18\n"
        "<!-- image -->\n"
        "# 个人信息出境标准合同\n"
        "| 数据类别 | 数据子类 |\n"
        "|----------|----------|\n"
        "| 个人信息 | 姓名、联系方式 |\n"
        "正文内容。\n"
    )

    result = clean_text(raw)

    assert "Number of Pages" not in result.text
    assert "<!-- image -->" not in result.text
    assert "|----------|----------|" not in result.text
    assert "| 数据类别 | 数据子类 |" in result.text
    assert "正文内容。" in result.text
    assert result.rule_hits["pdf_page_count_lines"] == 1
    assert result.rule_hits["image_placeholder_lines"] == 1
    assert result.rule_hits["table_divider_lines"] == 1


def test_clean_text_removes_pdf_cover_source_metadata() -> None:
    raw = (
        "2024 年 11 月\n"
        "本文档可从以下网址获得：\n"
        "[www.tc260.org.cn/](http://www.tc260.org.cn/)\n"
        "## 前 言\n"
        "本文件按照 GB/T 1.1 的规定起草。\n"
        "2024年11月1日起实施。\n"
    )

    result = clean_text(raw)

    assert "本文档可从以下网址获得" not in result.text
    assert "tc260.org.cn" not in result.text
    assert "2024 年 11 月" not in result.text
    assert "2024年11月1日起实施。" in result.text
    assert "本文件按照 GB/T 1.1 的规定起草。" in result.text
    assert result.rule_hits["standalone_year_month_lines"] == 1
    assert result.rule_hits["source_availability_lines"] == 1
    assert result.rule_hits["standalone_source_url_lines"] == 1


def test_clean_text_removes_pdf_cover_ocr_fragments() -> None:
    raw = (
        "## 网络安全标准实践指南\n"
        "## -个人信息跨境处理活动安全认证规范\n"
        "- -网络数据分类分级指引\n"
        "--粤港澳大湾区个人信息跨境处理保护要求\n"
        "- --修改了'征得授权同意的例外'（见 5.6）。\n"
        "TECHNICAL COMMITTEE\n"
        "Cyber\n"
        "委\n"
        "## 前 言\n"
        "本文件用于提供标准化实践指引。\n"
    )

    result = clean_text(raw)

    # Dash-prefixed titles are left for targeted/manual cleanup because a
    # generic rule can damage ordinary legal bullet lists.
    assert "## -个人信息跨境处理活动安全认证规范" in result.text
    assert "- -网络数据分类分级指引" in result.text
    assert "--粤港澳大湾区个人信息跨境处理保护要求" in result.text
    assert "- --修改了'征得授权同意的例外'" in result.text
    assert "TECHNICAL COMMITTEE" not in result.text
    assert "Cyber" not in result.text
    assert "\n委\n" not in result.text
    assert result.rule_hits["pdf_cover_fragment_lines"] == 3
