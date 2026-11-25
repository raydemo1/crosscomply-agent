from law_agent.data.pdf_layout import _add_rows, _join_lines, _Table, normalize_spacing


def test_spaces_between_cjk_glyphs_are_removed_but_section_numbers_keep_theirs() -> None:
    assert normalize_spacing("中国（重庆）数据出境管理清单（负 面清单）") == "中国（重庆）数据出境管理清单（负面清单）"
    assert normalize_spacing("自当年 1 月 1 日起累计提供 10 万人以上") == "自当年1月1日起累计提供10万人以上"
    assert normalize_spacing("3.12 转让 transfer of control") == "3.12 转让 transfer of control"


def _line(text: str, x1: float) -> dict:
    return {"text": text, "x0": 0.0, "x1": x1, "top": 0.0}


def test_lines_wrapped_at_the_margin_are_rejoined_and_new_items_are_not() -> None:
    lines = [
        _line("汽车数据处理者向境外提供汽车数据，符合以下情形之", 400),
        _line("一的，应当申报数据出境安全评估：", 250),
        _line("（1）向境外提供重要数据；", 200),
        _line("（2）自当年1月1日起累计向境外提供100万人以", 400),
        _line("上个人信息。", 100),
    ]
    assert _join_lines(lines) == [
        "汽车数据处理者向境外提供汽车数据，符合以下情形之一的，应当申报数据出境安全评估：",
        "（1）向境外提供重要数据；",
        "（2）自当年1月1日起累计向境外提供100万人以上个人信息。",
    ]


def test_merged_cells_take_the_value_above_and_page_breaks_stitch_rows() -> None:
    table = _Table(ncols=3)
    _add_rows(table, [
        ["医药行业", None, None],
        ["数据类别", "数据子类", "数据基本特征与描述"],
        ["个人信息", "7.累计向境外提供5万人以上的受试者", "仅限于：临床"],
        [None, "8.累计向境外提供10万人以上的患者信息。", "仅限于：药物警戒"],
    ], continued=False)
    _add_rows(table, [
        ["数据类别", "数据子类", "数据基本特征与描述"],  # header repeated on the next page
        ["", "", "场景。"],  # the rest of row 8, split by the page break
        [None, "9.累计向境外提供20万人以上的医疗卫生专业人士。", "仅限于：临床试验"],  # a new row
    ], continued=True)

    assert table.rows == [
        ["医药行业"],
        ["数据类别", "数据子类", "数据基本特征与描述"],
        ["个人信息", "7.累计向境外提供5万人以上的受试者", "仅限于：临床"],
        ["个人信息", "8.累计向境外提供10万人以上的患者信息。", "仅限于：药物警戒场景。"],
        ["个人信息", "9.累计向境外提供20万人以上的医疗卫生专业人士。", "仅限于：临床试验"],
    ]
