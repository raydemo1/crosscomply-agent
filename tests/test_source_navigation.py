"""Governed tree traversal, paging and checkpoint contracts without services."""

from copy import deepcopy
from datetime import date
from threading import Barrier, Event, get_ident

import pytest
from pydantic import ValidationError

from law_agent.review.agent import (
    INVESTIGATION_TOOLS,
    AgentState,
    AgentStep,
    EvidenceRead,
    EvidenceReadRequest,
    _read_evidence_batch,
    _read_evidence_request,
    review_context,
)
from law_agent.review.agent_context import search_observation_context
from law_agent.review.agent_tools import PREVIEW_CHARS
from law_agent.review.qa_agent import QAState
from law_agent.review.qa_context import qa_state_context
from law_agent.review.remediation import (
    RemediationAgentModel,
    RemediationDecision,
    execute_rereview,
)
from law_agent.review.retrieval.neighbors import hit_from_chunk
from law_agent.review.schemas import RetrievalQuery, ReviewFacts
from law_agent.review.source_navigation import SourceNavigation
from tests.test_remediation_assessment import _draft, _packet
from tests.test_review_agent import _chunk, _hit, _search_tools, _tools_with
from tests.test_review_llm import FakeClient


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


def test_remediation_registers_shared_search_and_batch_read_tools():
    model = RemediationAgentModel(model_id='test', client=FakeClient())
    tools = {t['function']['name']: t['function'] for t in model.protocol.tools}
    assert 'search_web' not in tools
    for name in ['search_evidence', 'read_evidence']:
        assert tools[name]['description'] == INVESTIGATION_TOOLS[name].description
    schema = tools['read_evidence']['parameters']['properties']['read_requests']
    assert schema['maxItems'] == 4
    with pytest.raises(ValidationError):
        RemediationDecision(action='read_evidence', summary='读原文')
    with pytest.raises(ValidationError):
        RemediationDecision(action='finish', summary='交付', draft=_draft(),
                            read_requests=[EvidenceReadRequest(source_id='s1', article_no='第二条')])


def test_remediation_default_tools_deliver_navigation_and_read_original(monkeypatch):
    from law_agent.review import remediation

    chunks = [_chunk('main', '第一条', '正文' * 80, index=0),
              _chunk('candidate', '第二条', '需要核对的适用条件。' * 30, index=1)]
    hits = [hit_from_chunk(c, i).model_copy(update={'score': 100 - i}) for i, c in enumerate(chunks)]
    tools = _search_tools(chunks, lambda *_a, **_kw: [hits], top_k=1)
    closed = []
    tools.close = lambda: closed.append(True)
    monkeypatch.setattr(remediation, 'ComplianceAgentTools', lambda **_: tools)

    def decide(state, _intake):
        if not state.steps:
            return RemediationDecision(action='search_evidence', summary='核对法律条件', max_chunks=1,
                queries=[RetrievalQuery(query_id='q', text='适用条件', query_type='legal_issue')])
        if not state.reads:
            source = state.steps[0].observation['results'][0]
            assert source['locations'][0]['acquired'] and source['locations'][0]['chunk_id'] == 'main'
            location = source['locations'][1]
            assert location['chunk_id'] == 'candidate'
            assert location['preview'] == chunks[1].text[:PREVIEW_CHARS]
            assert location['query_matches'][0]['rank'] == 2
            assert {h.chunk_id for h in state.evidence} == {'main'}
            return RemediationDecision(action='read_evidence', summary='取得候选原文',
                read_requests=[EvidenceReadRequest(source_id=source['source_id'], chunk_id=location['chunk_id'])])
        assert [h.chunk_id for h in state.evidence] == ['main', 'candidate']
        return RemediationDecision(action='finish', summary='仍需实施证明', draft=_draft(
            status='insufficient_evidence', confirmed_points=[], remaining_gaps=['缺少实施材料'],
            next_request='请提供设置截图。'))

    result = execute_rereview(packet=_packet(), goal='复核', state=None, model_id='test',
                             chunks_path='unused', decide=decide)
    assert result.status == 'completed' and result.reads == 1
    assert result.result['status'] == 'insufficient_evidence'
    assert closed == [True]


@pytest.mark.parametrize("qa", [False, True])
def test_source_directory_entry_is_directly_readable_in_review_and_qa(qa):
    chunks = [chunk(0, ["适用范围"], can_cite_clause=False), chunk(1, ["业务表格"], can_cite_clause=False)]
    tools = _tools_with(chunks)
    state_type = QAState if qa else AgentState
    state = state_type(goal="查原文", evidence=[hit_from_chunk(chunks[0], 0)])
    before = deepcopy(state.model_dump(mode="json"))
    context = qa_state_context(deepcopy(before)) if qa else review_context(state, {})["state"]
    source = context["evidence_sources"]["s1"]
    request = EvidenceReadRequest.model_validate(source["outline_read_request"])
    assert request.source_id == "s1" and request.node_id is None
    assert state.model_dump(mode="json") == before
    result = _read_evidence_request(state, request, read_evidence=tools.read_evidence, checkpoint=lambda _: None)
    assert result["read_mode"] == "outline" and result["chunk_ids"] == []
    assert [entry["title"] for entry in result["outline"]] == ["适用范围", "业务表格"]
    assert len(state.evidence) == 1 and state.evidence[0].can_cite_clause is False
    text = _read_evidence_request(state, EvidenceReadRequest(source_id="s1", node_id=result["outline"][1]["node_id"]),
                                  read_evidence=tools.read_evidence, checkpoint=lambda _: None)
    assert text["chunk_ids"] == ["c1"] and state.evidence[-1].can_cite_clause is False


def test_unread_keyword_source_offers_the_same_directory_entry_without_citing_preview():
    navigation = [{
        "source_id": "s1", "title": "文件", "doc_type": "guideline", "source_has_articles": False,
        "outline_read_request": {"source_id": "s1"},
        "locations": [{"chunk_id": "c0", "article_no": None, "preview": "仅导航预览",
                       "query_matches": [{"query_id": "q", "rank": 1}]}],
    }]
    state = AgentState(goal="查来源", steps=[AgentStep(number=1, action="search_evidence", summary="发现来源",
        observation={"results": navigation})])
    tools = _tools_with([chunk(0, ["原文目录"], can_cite_clause=False)])
    request = EvidenceReadRequest.model_validate(navigation[0]["outline_read_request"])
    result = _read_evidence_request(state, request, read_evidence=tools.read_evidence, checkpoint=lambda _: None)
    assert result["read_mode"] == "outline"
    assert state.evidence == []
    assert set(navigation[0]) == {"source_id", "title", "doc_type", "source_has_articles", "outline_read_request",
                                  "locations"}


def test_results_keep_per_query_top8_and_shared_memberships():
    chunks = [_chunk(f'c{i}', f'第{i}条', f'正文{i}' * 100, index=i) for i in range(10)]
    hits = [hit_from_chunk(c, i).model_copy(update={'score': 10 - i}) for i, c in enumerate(chunks)]
    tools = _search_tools(chunks, lambda *_a, **_kw: [hits, hits], top_k=1)
    queries = [RetrievalQuery(query_id=q, query_type='legal_issue', text=q) for q in ['q1', 'q2']]
    tools.search(queries, ReviewFacts())
    locations = [loc for source in tools.search_results() for loc in source['locations']]
    assert [loc['chunk_id'] for loc in locations] == [f'c{i}' for i in range(8)]
    assert locations[0]['acquired'] and all('preview' in loc for loc in locations[1:])
    assert locations[6]['query_matches'] == [{'query_id': 'q1', 'rank': 7}, {'query_id': 'q2', 'rank': 7}]


def test_parallel_reads_merge_in_request_order_and_keep_partial_success():
    hit = hit_from_chunk(_chunk('known', '第一条', '原文', index=0), 0)
    state = AgentState(goal='核对', evidence=[hit], max_reads=4)
    main_thread = get_ident()
    checkpoint_threads = []
    barrier = Barrier(3)
    completed_second = Event()

    def read(source, article, chunk, facts, offset):
        assert get_ident() != main_thread
        barrier.wait(timeout=3)
        if article == '第一条':
            assert completed_second.wait(timeout=3)
        if article == '第二条':
            completed_second.set()
        if article == '第三条':
            raise ValueError('条文不存在')
        return EvidenceRead(hits=[hit.model_copy(update={'chunk_id': article})])

    requests = [EvidenceReadRequest(source_id='s1', article_no=a) for a in ['第一条', '第二条', '第三条']]
    results = _read_evidence_batch(state, requests + [requests[0]], read_evidence=read,
                                   checkpoint=lambda _: checkpoint_threads.append(get_ident()))
    assert [r.get('chunk_ids') for r in results[:2]] == [['第一条'], ['第二条']]
    assert '条文不存在' in results[2]['error']
    assert '重复' in results[3]['error']
    assert state.reads == 3
    assert [r.returned_chunk_ids for r in state.read_history] == [['第一条'], ['第二条']]
    assert [h.chunk_id for h in state.evidence] == ['known', '第一条', '第二条']
    assert set(checkpoint_threads) == {main_thread}


def test_read_budget_is_reserved_before_parallel_dispatch():
    hit = hit_from_chunk(_chunk('known', '第一条', '原文', index=0), 0)
    state = AgentState(goal='核对', evidence=[hit], max_reads=1)
    calls = []

    def read(*args):
        calls.append(args[1])
        return EvidenceRead(hits=[hit])

    results = _read_evidence_batch(state, [EvidenceReadRequest(source_id='s1', article_no=a)
                                          for a in ['第一条', '第二条']], read_evidence=read, checkpoint=lambda _: None)
    assert calls == ['第一条'] and state.reads == 1
    assert '预算已用尽' in results[1]['error']


def test_query_association_update_does_not_repeat_preview():
    from law_agent.review.agent import AgentStep

    old = {'source_id': 's', 'title': '法', 'locations': [{'chunk_id': 'c', 'preview': '原文',
           'query_matches': [{'query_id': 'q1', 'rank': 6}]}]}
    new = {**old, 'locations': [{**old['locations'][0], 'query_matches': [{'query_id': 'q2', 'rank': 2}]}]}
    steps = [AgentStep(number=1, action='search_evidence', summary='发现', observation={'results': [old]})]
    result = search_observation_context({'results': [new]}, steps, [])
    loc = result['results'][0]['locations'][0]
    assert loc['query_matches'] == [{'query_id': 'q2', 'rank': 2}] and 'preview' not in loc
