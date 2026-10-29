"""Governed tree traversal, paging and checkpoint contracts without services."""

from datetime import date

import pytest

from law_agent.review.agent import AgentState, EvidenceReadRequest, _read_evidence_request
from law_agent.review.schemas import ReviewFacts
from law_agent.review.source_navigation import SourceNavigation
from tests.test_review_agent import _chunk, _hit, _tools_with


def chunk(index, path, *, article="", **updates):
    return _chunk(f"c{index}", article, f"原文{index}", index=index).model_copy(update={
        "heading_path": ["文件", *path], "title": "文件", **updates,
    })


def tree(chunks, as_of=date(2026, 10, 19)):
    return SourceNavigation(chunks, chunks, as_of)


def test_contiguous_prefixes_repeated_headings_and_direct_ownership():
    chunks = [chunk(0, ["甲", "小节"]), chunk(1, ["甲", "小节"]),
              chunk(2, ["乙", "小节"]), chunk(3, ["甲", "小节"]), chunk(4, [])]
    nav = tree(chunks)
    roots = nav.children(None)
    assert [r.title for r in roots] == ["甲", "乙", "甲", "正文片段 5"]
    assert roots[0].node_id != roots[2].node_id
    assert roots[0].chunk_count == 2
    assert [c.chunk_id for c in nav.node(roots[0].node_id).chunks] == ["c0", "c1"]
    owned = [cid for node in nav.nodes.values() for cid in node.direct_chunk_ids]
    assert sorted(owned) == [c.chunk_id for c in chunks]
    assert len(owned) == len(set(owned))


def test_article_and_table_paths_and_document_boundaries():
    chunks = [chunk(0, [], article="第十三条"),
              chunk(1, ["指南", "表格1"], can_cite_clause=False),
              chunk(2, ["指南", "表格1"], doc_id="other", can_cite_clause=False)]
    nav = tree(chunks)
    assert [r.title for r in nav.children(None)] == ["第十三条", "指南", "指南"]
    assert nav.children(None)[0].article_no == "第十三条"
    tools = _tools_with(chunks)
    roots = tools.read_evidence("s1", None, None, ReviewFacts()).outline
    read = tools.read_evidence("s1", None, None, ReviewFacts(), node_id=roots[1].node_id)
    assert [h.chunk_id for h in read.hits] == ["c1"]
    assert read.hits[0].can_cite_clause is False


def test_source_title_removed_repeatedly_and_id_invalidation():
    chunks = [chunk(0, ["文件", "甲"])]
    nav = tree(chunks)
    node_id = nav.roots[0]
    assert nav.nodes[node_id].heading_path == ["甲"]
    assert tree(list(reversed(chunks))).roots == nav.roots
    assert tree(chunks, date(2026, 10, 20)).roots != nav.roots
    for change in ({"text": "新原文"}, {"source_id": "s2"}, {"law_status": "unknown"}):
        changed = tree([chunks[0].model_copy(update=change)])
        with pytest.raises(ValueError, match="重新浏览"):
            changed.node(node_id)


def test_nested_outline_and_section_paging_share_temporal_view():
    chunks = [chunk(i, ["章", f"节{i}"]) for i in range(34)]
    chunks.append(chunk(34, ["未来"], publish_date="2028-01-01"))
    tools = _tools_with(chunks)
    facts = ReviewFacts(as_of_date="2026-10-19")
    root = tools.read_evidence("s1", None, None, facts)
    assert len(root.outline) == 1 and root.hits == []
    parent = root.outline[0].node_id
    first = tools.read_evidence("s1", None, None, facts, parent_node_id=parent)
    rest = tools.read_evidence("s1", None, None, facts, first.next_offset, parent_node_id=parent)
    assert len(first.outline) == 30 and len(rest.outline) == 4
    assert all(e.parent_node_id == parent and len(e.preview) <= 180 for e in first.outline)
    returned = []
    offset = 0
    while True:
        page = tools.read_evidence("s1", None, None, facts, offset, node_id=parent)
        returned.extend(h.chunk_id for h in page.hits)
        if page.next_offset is None:
            break
        offset = page.next_offset
    assert returned == [f"c{i}" for i in range(34)]
    with pytest.raises(ValueError, match="重新浏览"):
        tools.read_evidence("s1", None, None, ReviewFacts(as_of_date="2026-10-20"), node_id=parent)
    with pytest.raises(ValueError, match="超出"):
        tools.read_evidence("s1", None, None, facts, 35, node_id=parent)


@pytest.mark.parametrize("selectors", [
    {"node_id": "x", "article_no": "第一条"},
    {"node_id": "x", "parent_node_id": "y"},
    {"parent_node_id": "x", "chunk_id": "c0"},
    {"article_no": "第一条", "chunk_id": "c0"},
])
def test_conflicting_selectors_rejected(selectors):
    with pytest.raises(ValueError, match="最多指定一个"):
        EvidenceReadRequest(source_id="s1", **selectors)
    with pytest.raises(ValueError, match="最多指定一个"):
        _tools_with([chunk(0, [])]).read_evidence(
            "s1", selectors.get("article_no"), selectors.get("chunk_id"), ReviewFacts(),
            **{k: v for k, v in selectors.items() if k.endswith("node_id")},
        )


def test_invalid_offset_and_foreign_nodes():
    chunks = [chunk(0, ["甲"]), chunk(1, ["乙"], source_id="s2")]
    tools = _tools_with(chunks)
    facts = ReviewFacts()
    node_id = tools.read_evidence("s1", None, None, facts).outline[0].node_id
    for selector in ("node_id", "parent_node_id"):
        with pytest.raises(ValueError, match="重新浏览"):
            tools.read_evidence("s2", None, None, facts, **{selector: node_id})
    with pytest.raises(ValueError, match="offset"):
        tools.read_evidence("s1", None, None, facts, -1)
    with pytest.raises(ValueError, match="不能为空"):
        tools.read_evidence("s1", None, None, facts, node_id="")
    with pytest.raises(ValueError, match="不能为空"):
        EvidenceReadRequest(source_id="s1", parent_node_id=" ")


def test_history_resume_overlap_continuation_and_budget():
    chunks = [chunk(i, ["章", "节"]) for i in range(7)]
    tools = _tools_with(chunks)
    state = AgentState(goal="审查", evidence=[_hit("found", "s1")])
    assert state.max_reads == 10

    def read(request):
        return _read_evidence_request(state, request, read_evidence=tools.read_evidence,
                                      checkpoint=lambda _: None)

    outline = read(EvidenceReadRequest(source_id="s1"))
    assert outline["read_mode"] == "outline" and not outline["truncated"]
    assert outline["next_read_request"] is None
    parent = outline["outline"][0]["node_id"]
    assert [h.chunk_id for h in state.evidence] == ["found"]
    first = read(EvidenceReadRequest(source_id="s1", node_id=parent))
    assert first["read_mode"] == "text" and first["truncated"]
    assert first["next_read_request"]["node_id"] == parent
    state = AgentState.model_validate(state.model_dump(mode="json"))
    assert state.read_history[-1].next_offset == 5
    assert state.read_history[0].returned_node_ids == [parent]
    reads = state.reads
    assert "error" in read(EvidenceReadRequest(source_id="s1", node_id=parent))
    assert state.reads == reads
    last = read(EvidenceReadRequest.model_validate(first["next_read_request"]))
    assert not last["truncated"] and last["next_offset"] is None and last["next_read_request"] is None
    children = read(EvidenceReadRequest(source_id="s1", parent_node_id=parent))
    leaf = children["outline"][0]["node_id"]
    overlap = read(EvidenceReadRequest(source_id="s1", node_id=leaf))
    assert overlap["chunk_ids"] == [f"c{i}" for i in range(5)]
    assert len(state.evidence) == 8
    state.reads = 10
    with pytest.raises(ValueError, match="预算"):
        read(EvidenceReadRequest(source_id="s1", node_id=leaf, offset=5))
    old = AgentState.model_validate({**state.model_dump(), "max_reads": 8})
    assert old.max_reads == 8


def test_failed_tools_consume_budget_unknown_sources_do_not():
    state = AgentState(goal="审查", evidence=[_hit("found", "s1")])
    tools = _tools_with([chunk(0, ["甲"])])
    with pytest.raises(ValueError, match="重新浏览"):
        _read_evidence_request(state, EvidenceReadRequest(source_id="s1", node_id="bad"),
                               read_evidence=tools.read_evidence, checkpoint=lambda _: None)
    assert state.reads == 1 and state.read_history == []
    with pytest.raises(ValueError, match="本次已检索"):
        _read_evidence_request(state, EvidenceReadRequest(source_id="s2"),
                               read_evidence=tools.read_evidence, checkpoint=lambda _: None)
    assert state.reads == 1


def test_empty_leaf_outline_is_still_navigation():
    tools = _tools_with([chunk(0, ["甲"])])
    facts = ReviewFacts()
    node_id = tools.read_evidence("s1", None, None, facts).outline[0].node_id
    state = AgentState(goal="审查", evidence=[_hit("found", "s1")])
    obs = _read_evidence_request(
        state, EvidenceReadRequest(source_id="s1", parent_node_id=node_id),
        read_evidence=tools.read_evidence, checkpoint=lambda _: None,
    )
    assert obs["outline"] == [] and obs["read_mode"] == "outline"
    assert "不是完整原文或引用证据" in obs["instruction"]


def test_ten_actual_reads_and_partial_batch_exhaustion_are_checkpointed():
    from law_agent.review.agent import AgentDecision, run_agent
    from tests.test_review_agent import _draft

    tools = _tools_with([chunk(i, [f"节{i}"]) for i in range(12)])
    facts = ReviewFacts(as_of_date="2026-10-19")
    nodes = tools.read_evidence("s1", None, None, facts).outline
    decisions = iter([
        *(AgentDecision(action="read_evidence", summary="精读", read_requests=[
            EvidenceReadRequest(source_id="s1", node_id=node.node_id) for node in nodes[i:i + 4]
        ]) for i in range(0, 12, 4)),
        AgentDecision(action="finish", summary="交付", draft=_draft()),
    ])
    checkpoints = []
    state = run_agent(
        AgentState(goal="审查", facts=facts, evidence=[_hit("found", "s1")]),
        material="", intake={}, decide=lambda *_: next(decisions), search=lambda *_, **_kwargs: [],
        read_evidence=tools.read_evidence, web_search=lambda *_: [],
        finalize=lambda *_: {"ok": True},
        checkpoint=lambda s: checkpoints.append(s.model_dump(mode="json")),
    )
    assert state.reads == len(state.read_history) == 10
    results = state.steps[2].observation["read_results"]
    assert [r["chunk_ids"] for r in results[:2]] == [["c8"], ["c9"]]
    assert all("预算" in r["error"] for r in results[2:])
    restored = AgentState.model_validate(checkpoints[-1])
    assert restored.read_history[-1].node_id == nodes[9].node_id
