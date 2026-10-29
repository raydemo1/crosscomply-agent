import json
from datetime import date

import pytest

from law_agent.review.agent import (
    AgentState,
    AgentStep,
    EvidenceReadRequest,
    _read_evidence_request,
)
from law_agent.review.agent_context import search_observation_context
from law_agent.review.qa_agent import (
    QADateContext,
    QADecision,
    QAState,
    execute_qa,
    prepare_citations,
)
from law_agent.review.qa_context import qa_state_context
from law_agent.review.qa_store import InMemoryQAStore
from law_agent.review.retrieval.boosts import outside_explicit_scope
from law_agent.review.retrieval.neighbors import hit_from_chunk
from law_agent.review.schemas import RetrievalQuery, ReviewFacts
from tests.test_qa import StreamingClient, Tools
from tests.test_review_agent import _chunk, _search_tools
from tests.test_review_llm import tool_message


def query(text="规则", query_id="q"):
    return RetrievalQuery(query_id=query_id, text=text, query_type="legal_issue")


@pytest.mark.parametrize("linked", [False, True])
def test_search_keeps_query_specific_hit_without_requiring_neighbors(linked):
    main = _chunk("main", "第一条", "一般规则" * 50, index=1,
                  prev="before" if linked else None, next_id="after" if linked else None)
    before = _chunk("before", "第二条", "相邻背景", index=0)
    after = _chunk("after", "第三条", "相邻定义", index=2)
    common = _chunk("common", "第四条", "共同说明" * 50, index=3)
    repeated = _chunk("repeated", "第五条", "重复说明" * 50, index=4)
    specific = _chunk("specific", "第六条", "定向条件" * 50, index=8)
    chunks = [main, before, after, common, repeated, specific]

    def hits(values):
        return [hit_from_chunk(c, i).model_copy(update={"score": 100 - i}) for i, c in enumerate(values)]

    batches = [hits([main, specific, common, repeated]), hits([main, common, repeated])]
    tools = _search_tools(chunks, lambda *_a, **_kw: batches, top_k=10)
    result = tools.search([query("条件", "q1"), query("一般规则", "q2")], ReviewFacts())

    assert "specific" in {h.chunk_id for h in result}
    assert not {"before", "after"} & {h.chunk_id for h in result}
    assert len(result) == 4
    target = next(h for h in result if h.chunk_id == "specific")
    assert target.text == specific.text
    assert target.can_cite_clause == specific.can_cite_clause
    assert target.matched_query_ids == ["q1"]


def test_search_assembles_the_matched_article_without_adding_adjacent_clauses():
    before = _chunk("before", "第二条", "无关相邻条文", index=0)
    main = _chunk("main", "第一条", "第一条 一般条件。", index=1, prev="before", next_id="rest")
    rest = _chunk("rest", "第一条", "但符合例外的除外。", index=2, prev="main")
    tools = _search_tools([before, main, rest], lambda *_a, **_kw: [[hit_from_chunk(main, 0)]], top_k=10)

    result = tools.search([query()], ReviewFacts())

    assert [h.chunk_id for h in result] == ["main"]
    assert result[0].full_article_text == "第一条 一般条件。\n但符合例外的除外。"
    explicit = tools.read_evidence("s1", "第二条", None, ReviewFacts())
    assert [h.chunk_id for h in explicit.hits] == ["before"]


def test_unselected_short_clause_remains_navigation_without_an_untracked_full_quote():
    main = _chunk("main", "第一条", "一般规则" * 50, index=0)
    common = _chunk("common", "第二条", "共同说明" * 50, index=1)
    repeated = _chunk("repeated", "第三条", "重复说明" * 50, index=2)
    specific = _chunk("aux0", "第四条", "说明", index=3).model_copy(update={
        "can_cite_clause": False, "citation_role": "implementation_reference",
    })
    target = _chunk("target", "第五条", "短条文", index=0).model_copy(update={"source_id": "other"})

    def hits(chunks):
        return [hit_from_chunk(c, i).model_copy(update={"score": 100 - i}) for i, c in enumerate(chunks)]

    batches = [hits([main, specific, common, repeated, target]), hits([main, common, repeated, target])]
    tools = _search_tools([main, common, repeated, specific, target], lambda *_a, **_kw: batches, top_k=1)
    result = tools.search([query("定向问题", "q1"), query("一般问题", "q2")], ReviewFacts())

    assert "target" not in {h.chunk_id for h in result}
    location = next(loc for source in tools.search_navigation() for loc in source["locations"]
                    if loc["chunk_id"] == "target")
    assert location["article_no"] == "第五条"
    assert "preview" not in location
    assert tools.read_evidence("other", "第五条", None, ReviewFacts()).hits[0].text == "短条文"


def test_search_limit_requires_explicit_read_for_an_unselected_complete_article():
    main = _chunk("main", "第一条", "主要命中", index=0)
    target = _chunk("target", "第五十六条", "第五十六条 完整的条件、要求及例外。", index=0).model_copy(update={"source_id": "other"})
    hits = [hit_from_chunk(c, i) for i, c in enumerate([main, target])]
    tools = _search_tools([main, target], lambda *_a, **_k: [hits], top_k=1)
    qa = tools.search([query()], ReviewFacts(), coarse_scope=True)
    assert [h.chunk_id for h in qa] == ["main"]
    location = tools.search_navigation()[0]["locations"][0]
    assert location["chunk_id"] == "target" and "preview" not in location
    read = tools.read_evidence("other", "第五十六条", None, ReviewFacts())
    assert read.hits[0].text == target.text and read.hits[0].can_cite_clause == target.can_cite_clause
    citations, _ = prepare_citations(QAState(goal="q", evidence=qa + read.hits),
                                    QADecision(action="finish", summary="交付", chunk_ids=["target"]), [])
    assert citations[0]["excerpt"] == target.text
    assert [h.chunk_id for h in tools.search([query()], ReviewFacts())] == ["main"]


@pytest.mark.parametrize("coarse_scope", [False, True])
@pytest.mark.parametrize("mode", ["split_article", "truncated", "reference_only", "no_article"])
def test_navigation_does_not_automatically_admit_other_passages_or_upgrade_source_qualification(mode, coarse_scope):
    main = _chunk("main", "第一条", "主要命中", index=0)
    target = _chunk("target", "第五十六条", "短原文", index=0).model_copy(update={"source_id": "other"})
    chunks = [main, target]
    if mode == "split_article":
        chunks.append(target.model_copy(update={"chunk_id": "continuation", "chunk_index": 1, "text": "后续条件"}))
    elif mode == "truncated":
        chunks[-1] = target = target.model_copy(update={"text": "长原文" * 100})
    elif mode == "reference_only":
        chunks[-1] = target = target.model_copy(update={"can_cite_clause": False, "citation_role": "implementation_reference"})
    else:
        chunks[-1] = target = target.model_copy(update={"article_no": None})
    hits = [hit_from_chunk(c, i) for i, c in enumerate([main, target])]
    tools = _search_tools(chunks, lambda *_a, **_k: [hits], top_k=1)
    assert [h.chunk_id for h in tools.search([query()], ReviewFacts(), coarse_scope=coarse_scope)] == ["main"]
    assert tools.search_navigation()[0]["locations"][0]["chunk_id"] == "target"


def test_coarse_search_delivers_supporting_rules_without_requiring_extra_reads():
    main = _chunk("main", "第一条", "第一条 一般规则。", index=0, next_id="exception")
    exception = _chunk("exception", "第一条", "但满足条件时除外。", index=1, prev="main", next_id="related")
    related = _chunk("related", "第二条", "可能改变适用的相反依据。", index=2, prev="exception")
    other = _chunk("other", "", "其他来源的条件与例外。", index=0).model_copy(update={
        "source_id": "other-source", "can_cite_clause": False, "citation_role": "interpretation_auxiliary",
    })
    chunks = [main, exception, related, other]
    hits = [hit_from_chunk(c, i) for i, c in enumerate(chunks)]
    calls = []

    def search_many(*args, **kwargs):
        calls.append(args)
        return [hits]

    tools = _search_tools(chunks, search_many, top_k=10)
    delivered = tools.search([query()], ReviewFacts(), coarse_scope=True)
    assert len(calls) == 2
    assert {h.chunk_id for h in delivered} & {"main", "exception"}
    assert {"related", "other"} <= {h.chunk_id for h in delivered}
    assert len(delivered) == 3
    assert any("一般规则" in (h.full_article_text or "") and "但满足条件时除外" in h.full_article_text for h in delivered)
    assert {"main", "related", "other"} <= set(tools._candidate_hits)
    assert tools.search_navigation() == []
    state = QAState(goal="q", evidence=delivered)
    citations, _ = prepare_citations(state, QADecision(action="finish", summary="结束", chunk_ids=["other"]), [])
    assert citations[0]["kind"] == "context" and citations[0]["can_cite_clause"] is False
    assert "其他来源的条件与例外" in citations[0]["excerpt"]
    expanded = tools.search([query()], ReviewFacts())
    assert {"related", "other"} <= {h.chunk_id for h in expanded}
    assert any("一般规则" in (h.full_article_text or "") and "但满足条件时除外" in h.full_article_text for h in expanded)


def test_coarse_search_keeps_multi_query_sources_and_excludes_future_navigation():
    a = _chunk("a", "第一条", "一般规则", index=0)
    b = _chunk("b", "", "关键解释", index=0).model_copy(update={"source_id": "b-source"})
    future = _chunk("future", "第二条", "未来规则", index=1).model_copy(update={"publish_date": "2027-01-01"})
    hits = [hit_from_chunk(c, i) for i, c in enumerate([a, b, future])]
    tools = _search_tools([a, b, future], lambda *a, **k: [[hits[0], hits[2]], [hits[1]]], top_k=2)
    result = tools.search([query("一般规则", "q1"), query("解释", "q2")],
                          ReviewFacts(as_of_date="2026-10-08"), coarse_scope=True)
    assert {h.chunk_id for h in result} == {"a", "b"}
    assert "future" not in json.dumps(tools.search_navigation())
    assert "future" not in tools._candidate_hits


@pytest.mark.parametrize("role,region,subjects,facts,text,excluded", [
    ("conditional_local_basis", "CN-BJ", [], {"regions": ["上海"]}, "数据出境条件", True),
    ("conditional_local_basis", "CN-BJ", [], {"regions": ["上海"]}, "比较北京与上海的数据出境规则", False),
    ("conditional_local_basis", "CN-GD", [], {"regions": ["深圳"]}, "数据出境条件", False),
    ("conditional_local_basis", "CN-BJ", [], {"regions": ["德国"]}, "数据出境条件", False),
    ("conditional_local_basis", "unknown", [], {"regions": ["上海"]}, "数据出境条件", False),
    ("conditional_local_basis", "CN-XX", [], {"regions": ["上海"]}, "数据出境条件", False),
    ("conditional_local_basis", "CN-BJ", [], {"regions": ["CN-XX"]}, "数据出境条件", False),
    ("primary_legal_basis", "CN-BJ", [], {"regions": ["上海"]}, "数据出境条件", False),
    ("conditional_industry_basis", "CN", ["汽车数据处理者"], {"industry": "金融"}, "本行业的数据规则", True),
    ("conditional_industry_basis", "CN", ["汽车数据处理者"], {"industry": "金融"}, "比较汽车数据规则", False),
    ("conditional_industry_basis", "CN", ["汽车数据处理者"], {"industry": "车联网"}, "位置数据规则", False),
    ("conditional_industry_basis", "CN", ["汽车数据处理者"], {}, "位置数据规则", False),
    ("conditional_industry_basis", "CN", [], {"industry": "金融"}, "本行业的数据规则", False),
])
def test_coarse_scope_only_excludes_explicit_metadata_conflicts(role, region, subjects, facts, text, excluded):
    chunk = _chunk("scope", "第一条", "适用规则", index=0).model_copy(update={
        "citation_role": role, "applicable_region": region, "applicable_subjects": subjects,
    })
    assert outside_explicit_scope(chunk, ReviewFacts(**facts), text) is excluded


def test_coarse_scope_applies_to_both_retrievers_but_review_keeps_existing_results():
    national = _chunk("national", "第一条", "全国一般规定", index=0)
    local = _chunk("local", "第一条", "另一个地区的规定", index=0).model_copy(update={
        "source_id": "local-source", "citation_role": "conditional_local_basis", "applicable_region": "CN-BJ",
    })
    uncertain = _chunk("uncertain", "", "措辞不同但可能相关的解释", index=0).model_copy(update={
        "source_id": "uncertain-source", "citation_role": "interpretation_auxiliary", "can_cite_clause": False,
    })
    hits = [hit_from_chunk(c, i) for i, c in enumerate([national, local, uncertain])]
    tools = _search_tools([national, local, uncertain], lambda *a, **k: [hits], top_k=10)
    filtered = tools.search([query("上海数据出境")], ReviewFacts(regions=["上海"]), coarse_scope=True)
    assert {h.chunk_id for h in filtered} == {"national", "uncertain"}
    assert "local-source" not in json.dumps(tools.search_navigation())
    expanded = tools.search([query("上海数据出境")], ReviewFacts(regions=["上海"]))
    assert {h.chunk_id for h in expanded} == {"national", "local", "uncertain"}


def test_topic_tags_alone_do_not_establish_exclusive_industry_scope():
    chunk = _chunk("scope", "第一条", "相关资料", index=0).model_copy(update={
        "citation_role": "conditional_industry_basis", "applicable_subjects": [], "topic_tags": ["汽车"],
    })
    assert outside_explicit_scope(chunk, ReviewFacts(industry="金融"), "数据处理规则") is False


@pytest.mark.parametrize("role,region,title,subjects,excluded", [
    ("conditional_local_basis", "CN-SH", "某自由贸易试验区清单", [], True),
    ("interpretation_auxiliary", "CN-SH", "某自由贸易试验区清单答问", [], True),
    ("interpretation_auxiliary", "CN", "全国数据跨境规则", [], False),
    ("primary_legal_basis", "CN", "全国规则含自由贸易试验区条款", [], False),
    ("interpretation_auxiliary", "unknown", "可能相关的通用解释", [], False),
    ("conditional_industry_basis", "CN", "专项规定", ["自由贸易试验区企业"], True),
])
def test_grounded_scope_exclusion_only_matches_specialized_source_identity(role, region, title, subjects, excluded):
    chunk = _chunk("scope", "第一条", "正文即使提到自由贸易试验区也不作为排除依据", index=0).model_copy(update={
        "citation_role": role, "applicable_region": region, "title": title, "applicable_subjects": subjects,
    })
    assert outside_explicit_scope(chunk, ReviewFacts(), "员工数据出境", ["自由贸易试验区"]) is excluded
    assert outside_explicit_scope(chunk, ReviewFacts(), f"请解释《{title}》为何不适用", ["自由贸易试验区"]) is False


def test_excluded_scope_terms_apply_before_both_retrievers_and_leave_review_unchanged():
    national = _chunk("national", "第一条", "全国规则提及自由贸易试验区", index=0)
    local = _chunk("local", "第一条", "地方清单", index=0).model_copy(update={
        "source_id": "local", "title": "某自由贸易试验区清单", "citation_role": "conditional_local_basis", "applicable_region": "CN-SH",
    })
    hits = [hit_from_chunk(c, n) for n, c in enumerate([national, local])]
    tools = _search_tools([national, local], lambda *a, **k: [hits], top_k=10)
    result = tools.search([query()], ReviewFacts(), coarse_scope=True, excluded_scope_terms=["自由贸易试验区"])
    assert [h.chunk_id for h in result] == ["national"]
    assert tools.search_navigation() == []
    assert {h.chunk_id for h in tools.search([query()], ReviewFacts())} == {"national", "local"}


def test_scope_exclusions_are_grounded_in_selected_input_and_fail_as_tool_feedback():
    class ScopedTools(Tools):
        def __init__(self):
            self.seen = []

        def search(self, queries, facts, *, coarse_scope=False, excluded_scope_terms=(), max_chunks=None):
            self.seen.append(list(excluded_scope_terms))
            return super().search(queries, facts, coarse_scope=coarse_scope, excluded_scope_terms=excluded_scope_terms)

    def decision(call_id, quote):
        return tool_message((call_id, "search_evidence", {"summary": "检索", "queries": [query().model_dump()],
            "date_context": {"as_of_date": None}, "research_context": {"excluded_source_scopes": [{"terms": ["自由贸易试验区"], "quote": quote}]}}))

    client = StreamingClient(outputs=[decision("invalid", "模型推断不在自贸区"), decision("valid", "企业不在自贸区"),
        tool_message(("next", "search_evidence", {"summary": "补充", "queries": [query().model_dump()],
                                                "research_context": {"industry": "金融"}})),
        tool_message(("finish", "finish", {"summary": "完成", "chunk_ids": ["c1"]}))])
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    store.add_material(cid, {"id": "selected", "filename": "业务.txt", "parsed_text": "企业不在自贸区"})
    store.add_material(cid, {"id": "unselected", "filename": "无关.txt", "parsed_text": "模型推断不在自贸区"})
    run = store.enqueue(cid, question="能否适用？", material_ids=["selected"], request_id="1")
    tools = ScopedTools()
    updates = []
    execute_qa(store.get(cid), run, checkpoint=lambda **kw: updates.append(kw), chunks_path="unused", client=client, tools=tools)
    assert tools.seen == [["自由贸易试验区"], ["自由贸易试验区"]]
    initial = json.loads(client.calls[0][1].content)
    assert initial["material_passages"][0]["text"] == "企业不在自贸区"
    assert initial["material_reading"]["next_read_request"] is None
    assert "material_preview" not in initial["state"]
    feedback = next(json.loads(m.content) for m in client.answer_messages if m.role == "tool")
    assert "quote必须逐字来自" in feedback["observation"]["error"]
    state = [u["state"] for u in updates if "state" in u][-1]
    assert len(state["source_scope_exclusions"]) == 1
    assert updates[-1]["status"] == "completed"


@pytest.mark.parametrize("question", ["比较北京与上海的数据出境规则", "上海企业能参考《特定地方规则》吗"])
def test_coarse_search_preserves_sources_named_in_original_question(question):
    local = _chunk("local", "第一条", "地方规则正文", index=0).model_copy(update={
        "title": "特定地方规则", "citation_role": "conditional_local_basis", "applicable_region": "CN-BJ",
    })
    hit = hit_from_chunk(local, 0)
    tools = _search_tools([local], lambda *a, **k: [[hit]], top_k=1)
    tools._question = question
    result = tools.search([query("数据出境规则")], ReviewFacts(regions=["上海"]), coarse_scope=True)
    assert [h.chunk_id for h in result] == ["local"]


def test_navigation_only_candidates_still_require_reading():
    main = _chunk("main", "第一条", "一般规则", index=0)
    other = _chunk("other", "第一条", "可能改变结论的例外。" * 30, index=0).model_copy(update={"source_id": "other-source"})
    hits = [hit_from_chunk(c, i) for i, c in enumerate([main, other])]
    tools = _search_tools([main, other], lambda *a, **k: [hits], top_k=1)
    delivered = tools.search([query()], ReviewFacts(), coarse_scope=True)
    candidates = tools.search_navigation()
    assert candidates[0]["source_id"] == "other-source"
    state = QAState(goal="q", evidence=delivered, steps=[AgentStep(
        number=1, action="search_evidence", summary="检索", observation={"keyword_candidates": candidates},
    )])
    decision = QADecision(action="finish", summary="作答", chunk_ids=["other"])
    with pytest.raises(ValueError, match="本次调查已取得的正文"):
        prepare_citations(state, decision, [])
    _read_evidence_request(state, EvidenceReadRequest(source_id="other-source", chunk_id="other"),
                           read_evidence=tools.read_evidence, checkpoint=lambda _: None)
    assert prepare_citations(state, decision, [])[0][0]["excerpt"] == other.text


def test_navigation_projection_removes_repeats_and_read_locations_but_keeps_updates():
    from copy import deepcopy
    source = {"source_id": "s1", "title": "来源", "source_has_articles": True, "locations": [
        {"chunk_id": "unread", "article_no": "第一条", "preview": "待精读原文"},
    ]}
    old = {"keyword_candidates": [source]}
    previous = [AgentStep(number=1, action="search_evidence", summary="检索", observation=old)]
    observation = deepcopy(old)
    observation["keyword_candidates"][0]["locations"].append({"chunk_id": "new", "preview": "新候选"})
    snapshot = deepcopy(observation)
    projected = search_observation_context(observation, previous, [])
    assert [loc["chunk_id"] for loc in projected["keyword_candidates"][0]["locations"]] == ["new"]
    assert observation == snapshot and previous[0].observation == old
    observation["keyword_candidates"][0]["locations"][0]["preview"] = "原文预览已更新"
    projected = search_observation_context(observation, previous, [])
    assert len(projected["keyword_candidates"][0]["locations"]) == 2
    read = [hit_from_chunk(_chunk("new", "第二条", "已取得的原文", index=0), 0)]
    projected = search_observation_context(observation, previous, read)
    assert [loc["chunk_id"] for loc in projected["keyword_candidates"][0]["locations"]] == ["unread"]


def test_repeated_navigation_is_saved_but_not_resent_in_native_tool_results():
    class NavigationTools(Tools):
        def search_navigation(self):
            return [{"source_id": "other", "title": "其他来源", "source_has_articles": True, "locations": [
                {"chunk_id": "unread", "article_no": "第一条", "preview": "候选原文"},
            ]}]

    client = StreamingClient(outputs=[
        tool_message((f"search{index}", "search_evidence", {"summary": "检索", "queries": [query().model_dump()],
            **({"date_context": {"as_of_date": None}} if index == 0 else {})}))
        for index in range(2)
    ] + [tool_message(("finish", "finish", {"summary": "回答", "chunk_ids": ["c1"]}))])
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    run = store.enqueue(cid, question="条件", material_ids=[], request_id="1")
    updates = []
    execute_qa(store.get(cid), run, checkpoint=lambda **kw: updates.append(kw), chunks_path="unused",
               client=client, tools=NavigationTools())
    results = [json.loads(m.content)["observation"] for m in client.answer_messages if m.role == "tool"]
    assert results[0]["keyword_candidates"][0]["source_id"] == "other"
    assert results[1]["keyword_candidates"] == []
    saved = [u["state"]["steps"] for u in updates if "state" in u][-1]
    assert all(s["observation"]["keyword_candidates"] for s in saved)
    assert client.answer_messages[:len(client.calls[-1])] == client.calls[-1]


def test_qa_projection_only_exposes_explicit_search_hints_without_changing_review_context():
    state = QAState(goal="用户原问题", facts=ReviewFacts(regions=["北京"], as_of_date="2026-10-08"),
                    date_context=QADateContext(as_of_date="2026-10-08", quote="按2026-10-08回答"))
    original = state.model_dump(mode="json")
    context = qa_state_context(original)
    assert context["research_context"] == {"regions": ["北京"]}
    assert context["date_context"] == {"as_of_date": "2026-10-08", "quote": "按2026-10-08回答"}
    assert "facts" not in context and "ciio_status" not in json.dumps(context)
    assert original == state.model_dump(mode="json")
    assert "facts" in AgentState(goal="审查").model_dump()


def test_search_only_schema_and_historical_hints_reach_shared_tools():
    class RecordingTools(Tools):
        def __init__(self):
            self.seen = []

        def search(self, queries, facts, *, coarse_scope=False, excluded_scope_terms=(), max_chunks=None):
            assert coarse_scope is True
            self.seen.append(facts.model_copy(deep=True))
            return super().search(queries, facts, coarse_scope=coarse_scope)

    client = StreamingClient(outputs=[
        tool_message(("search1", "search_evidence", {"summary": "按用户时点检索",
            "queries": [query().model_dump()], "date_context": {"as_of_date": "2026-10-08", "quote": "按2026-10-08回答"}, "research_context": {"regions": ["北京"]}})),
        tool_message(("search2", "search_evidence", {"summary": "追加调查", "queries": [query().model_dump()],
            "research_context": {"industry": "金融"}})),
        tool_message(("finish", "finish", {"summary": "作答", "chunk_ids": ["c1"]})),
    ])
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    run = store.enqueue(cid, question="按2026-10-08回答", material_ids=[], request_id="1")
    tools = RecordingTools()
    updates = []
    execute_qa(store.get(cid), run, checkpoint=lambda **changes: updates.append(changes),
               chunks_path="unused", client=client, tools=tools)
    assert [f.as_of_date for f in tools.seen] == [date(2026, 10, 8)] * 2
    assert tools.seen[1].regions == ["北京"] and tools.seen[1].industry == "金融"
    schemas = {t["function"]["name"]: t["function"]["parameters"] for t in client.kwargs[0]["tools"]}
    assert "ReviewFacts" not in json.dumps(schemas)
    assert {name for name, s in schemas.items() if "research_context" in s["properties"]} == {"search_evidence"}
    assert {name for name, s in schemas.items() if "date_context" in s["properties"]} == {"search_evidence", "read_evidence", "search_web"}
    assert set(schemas["search_evidence"]["$defs"]["QASearchContext"]["properties"]) == {"regions", "industry", "excluded_source_scopes"}
    assert set(schemas["search_web"]["$defs"]["QADateContext"]["properties"]) == {"as_of_date", "quote"}
    assert "facts" not in json.loads(client.calls[0][1].content)["state"]
    assert client.answer_messages[:len(client.calls[-1])] == client.calls[-1]
    assert updates[-1]["status"] == "completed"


def test_changed_timepoint_retains_comparison_source_without_claiming_current_validity():
    future = _chunk("future", "第一条", "第一条 新规则。", index=0).model_copy(update={
        "publish_date": "2026-10-15", "effective_date": "2026-10-15", "law_status": "effective",
    })
    hit = hit_from_chunk(future, 0)
    tools = _search_tools([future], lambda *a, **k: [[hit]], top_k=1)
    client = StreamingClient(outputs=[
        tool_message(("later", "search_evidence", {"summary": "检索后期版本",
            "queries": [query().model_dump()], "date_context": {"as_of_date": "2026-10-20", "quote": "20日"}})),
        tool_message(("earlier", "search_evidence", {"summary": "对照早期时点",
            "queries": [query().model_dump()], "date_context": {"as_of_date": "2026-10-08", "quote": "10月8日"}})),
        tool_message(("finish", "finish", {"summary": "比较版本", "chunk_ids": ["future"]})),
    ])
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    run = store.enqueue(cid, question="比较10月8日和20日的规则", material_ids=[], request_id="1")
    updates = []
    execute_qa(store.get(cid), run, checkpoint=lambda **kw: updates.append(kw), chunks_path="unused",
               client=client, tools=tools)
    citation = updates[-1]["result"]["citations"][0]
    assert citation["retrieved_as_of_dates"] == ["2026-10-20"]
    assert citation["answer_as_of_date"] == "2026-10-08"
    assert citation["available_at_answer_date"] is False
    assert citation["can_cite_clause"] is False
    assert citation["kind"] == "context"
    assert "非回答时点有效依据" in updates[-1]["answer"]
    observations = [json.loads(m.content)["observation"] for m in client.answer_messages if m.role == "tool"]
    assert [o["as_of_date"] for o in observations[:-1]] == ["2026-10-20", "2026-10-08"]
    assert observations[-1]["citation_registry"][0]["available_at_answer_date"] is False
    assert updates[-1]["status"] == "completed"


def test_default_date_stays_fixed_across_midnight_and_explicit_reset(monkeypatch):
    from datetime import datetime

    from law_agent.review import dates

    clock_calls = []

    class Clock:
        @staticmethod
        def now(tz):
            clock_calls.append(tz)
            return datetime.fromisoformat("2026-10-20T15:59:00+00:00" if len(clock_calls) == 1
                                          else "2026-10-20T16:03:00+00:00")

    monkeypatch.setattr(dates, "datetime", Clock)

    class RecordingTools(Tools):
        def __init__(self):
            self.seen = []

        def search(self, queries, facts, **kwargs):
            self.seen.append(facts.as_of_date)
            return super().search(queries, facts, **kwargs)

    client = StreamingClient(outputs=[
        tool_message(("first", "search_evidence", {"summary": "调查当前规则", "queries": [query().model_dump()], "date_context": {"as_of_date": None}})),
        tool_message(("old", "search_evidence", {"summary": "历史对照", "queries": [query().model_dump()],
            "date_context": {"as_of_date": "2026-10-08", "quote": "2026-10-08的历史规则"}})),
        tool_message(("reset", "search_evidence", {"summary": "回到当前时点", "queries": [query().model_dump()],
            "date_context": {"as_of_date": None}})),
        tool_message(("finish", "finish", {"summary": "回答", "chunk_ids": ["c1"]})),
    ])
    store = InMemoryQAStore()
    cid = store.create("owner")["id"]
    run = store.enqueue(cid, question="比较当前和2026-10-08的历史规则", material_ids=[], request_id="1")
    tools = RecordingTools()
    execute_qa(store.get(cid), run, checkpoint=lambda **kw: None, chunks_path="unused", client=client, tools=tools)
    assert tools.seen == [date(2026, 10, 20), date(2026, 10, 8), date(2026, 10, 20)]
    assert len(clock_calls) == 1
    observations = [json.loads(m.content)["observation"] for m in client.answer_messages if m.role == "tool"]
    assert observations[-1]["citation_registry"][0]["retrieved_as_of_dates"] == ["2026-10-20", "2026-10-08"]


@pytest.mark.parametrize("recipient", ["香港", "澳门"])
def test_destination_alone_does_not_exclude_sources(recipient):
    chunk = _chunk("scope", "", "专项指引", index=0).model_copy(update={
        "title": f"内地与{recipient}专项合同指引", "citation_role": "interpretation_auxiliary",
        "applicable_region": "CN-GBA", "applicable_subjects": [f"{recipient}接收方"],
    })
    facts = ReviewFacts(destination_region="德国")
    assert outside_explicit_scope(chunk, facts, "员工数据同步到德国") is False
    hits = [hit_from_chunk(chunk, 0)]
    tools = _search_tools([chunk], lambda *a, **k: [hits], top_k=10)
    tools._question = "员工数据同步到德国"
    assert [h.chunk_id for h in tools.search([query()], facts, coarse_scope=True)] == ["scope"]


def test_generated_query_does_not_override_user_scope_but_user_comparison_does():
    chunk = _chunk("ftz", "", "地方清单", index=0).model_copy(update={
        "title": "某自由贸易试验区清单", "citation_role": "conditional_local_basis",
        "applicable_region": "CN-SH", "applicable_subjects": ["自贸试验区企业"],
    })
    facts = ReviewFacts()
    terms = ["自由贸易试验区"]
    assert outside_explicit_scope(chunk, facts, chunk.title, terms, requested_text="不在自贸区") is True
    assert outside_explicit_scope(chunk, facts, chunk.title, terms, requested_text=f"解释《{chunk.title}》") is False
    hits = [hit_from_chunk(chunk, 0)]
    tools = _search_tools([chunk], lambda *a, **k: [hits], top_k=10)
    tools._question = "不在自贸区"
    assert tools.search([query(chunk.title)], facts, coarse_scope=True, excluded_scope_terms=terms) == []
    assert tools.search_navigation() == []
    assert tools.search([query(chunk.title)], facts)
