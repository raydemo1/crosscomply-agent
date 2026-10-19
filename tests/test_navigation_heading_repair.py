"""A repaired boundary preserves exact source content and industry ownership."""

from scripts.repair_navigation_headings import move_trailing_heading, repair_paths
from tests.test_source_navigation import chunk, tree


def test_boundary_repair_moves_heading_without_changing_source_content():
    chunks = [
        chunk(0, ["六、医疗器械行业"], text="医疗器械正文\n七、自动驾驶行业（智能网联汽车）"),
        chunk(1, ["20 表格数据"], text="20 表格数据\n自动驾驶正文"),
        chunk(2, ["100 万人以上车主信息"], text="100 万人以上车主信息\n自动驾驶续文"),
        chunk(3, ["八、贸易物流行业"], text="八、贸易物流行业\n物流正文"),
    ]
    updated = repair_paths(move_trailing_heading(chunks, 0))
    assert "\n".join(c.text for c in updated) == "\n".join(c.text for c in chunks)
    roots = tree(updated).children(None)
    assert [(node.title, node.chunk_count) for node in roots] == [
        ("六、医疗器械行业", 1), ("七、自动驾驶行业（智能网联汽车）", 2), ("八、贸易物流行业", 1),
    ]
