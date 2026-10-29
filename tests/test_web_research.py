"""Web observations cannot become formal clause evidence."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

from law_agent.data.schemas import Chunk
from law_agent.review.agent import AgentDecision, AgentState, run_agent
from law_agent.review.remediation import RemediationDecision
from law_agent.review.schemas import RetrievalHit, RetrievalQuery
from law_agent.review.web_research import (
    DEFAULT_WEB_RESULTS,
    MAX_WEB_RESULTS,
    ExaSearchClient,
    WebFinding,
    WebResearch,
    WebSearchBatch,
    WebSearchResult,
    WebSearchUnavailable,
    build_web_search_client,
    is_official_url,
)
from tests.test_review_agent import _draft


def query() -> RetrievalQuery:
    return RetrievalQuery(query_id="q1", query_type="legal_issue", text="数据出境新规")


class FakeClient:
    def __init__(self, results: list[WebSearchResult]):
        self.results = results

    def search(self, _query: str, *, max_results: int) -> list[WebSearchResult]:
        return self.results[:max_results]


def test_official_content_is_visible_but_never_joins_evidence() -> None:
    finding = WebFinding(title="新规", url="https://www.cac.gov.cn/new.html", excerpt="新的办理要求")
    decisions = iter([
        AgentDecision(action="search_web", summary="查新规", queries=[query()]),
        AgentDecision(action="queue_enrichment", summary="补充相关官方来源", enrichment_urls=[finding.url]),
        AgentDecision(action="request_input", summary="等待", question="请补充事实"),
    ])
    enqueued: list[WebFinding] = []
    seen: list[AgentState] = []

    def decide(state, _rule):
        seen.append(state)
        return next(decisions)

    state = run_agent(
        AgentState(goal="审查"), material="", intake={},
        decide=decide, search=lambda *_, **_kwargs: [],
        web_search=lambda *_: WebSearchBatch(findings=[finding]), on_web_findings=enqueued.extend,
        finalize=lambda *_: {}, checkpoint=lambda *_: None,
    )
    assert state.evidence == []
    assert "web_findings" not in state.model_fields
    assert enqueued == [finding]
    assert state.steps[0].observation["findings"][0]["excerpt"] == "新的办理要求"
    assert state.steps[0].observation["queries"] == [query().model_dump(mode="json")]
    assert state.queries == []
    assert seen[1].steps[0].observation["findings"][0]["url"] == finding.url
    assert state.steps[1].observation["submitted_urls"] == [finding.url]


def test_web_search_alone_does_not_enqueue_irrelevant_official_result() -> None:
    finding = WebFinding(title="无关公告", url="https://www.cac.gov.cn/unrelated", excerpt="无关内容")
    decisions = iter([
        AgentDecision(action="search_web", summary="调查", queries=[query()]),
        AgentDecision(action="request_input", summary="等待", question="请补充事实"),
    ])
    enqueued: list[WebFinding] = []
    run_agent(
        AgentState(goal="审查"), material="", intake={},
        decide=lambda *_: next(decisions), search=lambda *_, **_kwargs: [],
        web_search=lambda *_: WebSearchBatch(findings=[finding]), on_web_findings=enqueued.extend,
        finalize=lambda *_: {}, checkpoint=lambda *_: None,
    )
    assert enqueued == []


def test_known_official_url_returns_governed_source_id() -> None:
    chunk = Chunk(
        chunk_id="old:1", doc_id="old", source_id="old", title="旧版",
        text="旧条款", chunk_index=0, source_url="https://www.cac.gov.cn/new",
        char_count=3,
    )
    findings = WebResearch(
        client=FakeClient([WebSearchResult(
            url="https://www.cac.gov.cn/new", title="新版",
            text="搜索摘录",
        )]),
        corpus_chunks=[chunk],
    ).search([query()]).findings
    assert findings[0].known_source_id == "old"
    assert findings[0].excerpt == "搜索摘录"
    assert findings[0].refresh_needed
    assert set(findings[0].model_dump()) == {
        "title", "url", "excerpt", "known_source_id", "published_date", "refresh_needed", "is_official",
        "excerpt_type", "excerpt_truncated", "excerpt_query_id", "published_after_as_of",
    }

    hit = RetrievalHit(
        chunk_id="known:1", doc_id="old", source_id="old", title="旧版",
        text="旧条款", score=1.0, rank=0, retriever="keyword",
        citation_role="primary_legal_basis", can_cite_clause=True,
        source_url="https://www.cac.gov.cn/new",
    )
    decisions = iter([
        AgentDecision(action="search_web", summary="发现来源", queries=[query()]),
        AgentDecision(action="search_evidence", summary="取正式证据", queries=[query()]),
        AgentDecision(action="request_input", summary="等待", question="请补充事实"),
    ])
    state = run_agent(
        AgentState(goal="审查"), material="", intake={}, decide=lambda *_: next(decisions),
        search=lambda *_, **_kwargs: [hit], web_search=lambda *_: WebSearchBatch(findings=findings),
        finalize=lambda *_: {}, checkpoint=lambda *_: None,
    )
    assert state.evidence == [hit]
    assert state.queries == [query().model_copy(update={"query_id": "2:0"})]
    assert state.steps[0].observation["findings"][0]["known_source_id"] == "old"


def test_exa_requests_query_highlights_without_summary() -> None:
    response = httpx.Response(200, json={"results": [{
        "url": "https://www.cac.gov.cn/new", "title": "新规", "highlights": ["正文"],
    }]}, request=httpx.Request("POST", "https://api.exa.ai/search"))
    with patch("law_agent.review.web_research.httpx.post", return_value=response) as post:
        results = ExaSearchClient(api_key="test").search("新规", max_results=2)
    payload = post.call_args.kwargs["json"]
    assert payload["contents"]["highlights"]["maxCharacters"] == 1200
    assert payload["contents"]["highlights"]["query"] == "新规"
    assert "text" not in payload["contents"]
    assert "summary" not in payload["contents"]
    assert "includeDomains" not in payload
    assert "excludeDomains" not in payload
    assert payload["numResults"] == 2
    assert post.call_args.kwargs["follow_redirects"] is False
    assert results[0].text == "正文"


def test_no_excerpt_does_not_trigger_page_fetch() -> None:
    findings = WebResearch(client=FakeClient([
        WebSearchResult(url="https://www.cac.gov.cn/new", title="新规"),
    ])).search([query()]).findings
    assert findings == [WebFinding(url="https://www.cac.gov.cn/new", title="新规", excerpt_query_id="q1")]


def test_open_discovery_keeps_other_sites_without_official_identity() -> None:
    findings = WebResearch(client=FakeClient([
        WebSearchResult(url="https://www.cac.gov.cn.evil.com/new", title="伪装域名"),
        WebSearchResult(url="https://www.cac.gov.cn@evil.com/new", title="用户名伪装"),
        WebSearchResult(url="https://www.cac.gov.cn/new", title="官方来源"),
    ])).search([query()]).findings
    assert {item.title for item in findings} == {"官方来源", "伪装域名"}
    assert next(item for item in findings if item.title == "官方来源").is_official
    assert not next(item for item in findings if item.title == "伪装域名").is_official
    assert WebResearch(client=FakeClient([
        WebSearchResult(url="https://[invalid", title="损坏 URL"),
    ])).search([query()]).findings == []


def test_newer_published_date_marks_existing_url_for_official_refresh() -> None:
    chunk = Chunk(
        chunk_id="old:1", doc_id="old", source_id="old", title="示例法",
        text="原有条款", chunk_index=0, source_url="https://flk.npc.gov.cn/law",
        char_count=4, publish_date="2024-01-01",
    )
    finding = WebResearch(client=FakeClient([
        WebSearchResult(url=chunk.source_url, title=chunk.title,
                        text="原有条款", published_date="2026-09-01"),
    ]), corpus_chunks=[chunk]).search([query()]).findings[0]
    assert finding.known_source_id == "old"
    assert finding.refresh_needed
    assert finding.published_date == "2026-09-01"


def test_known_url_with_refresh_signal_can_queue_official_check() -> None:
    finding = WebFinding(
        title="新版法规", url="https://flk.npc.gov.cn/law",
        excerpt="新版条款", known_source_id="old", refresh_needed=True,
    )
    decisions = iter([
        AgentDecision(action="search_web", summary="核实更新", queries=[query()]),
        AgentDecision(action="queue_enrichment", summary="核验官方原件", enrichment_urls=[finding.url]),
        AgentDecision(action="request_input", summary="等待", question="请补充事实"),
    ])
    queued = []
    run_agent(
        AgentState(goal="审查"), material="", intake={}, decide=lambda *_: next(decisions),
        search=lambda *_, **_kwargs: [], web_search=lambda *_: WebSearchBatch(findings=[finding]),
        on_web_findings=queued.extend, finalize=lambda *_: {}, checkpoint=lambda *_: None,
    )
    assert queued == [finding]


def test_web_action_limit_is_two_and_query_limit_is_four() -> None:
    from law_agent.review.agent import INVESTIGATION_TOOLS
    from law_agent.review.qa_agent import QADecision
    from law_agent.review.tool_calling import ToolCallingAgent

    queries = [RetrievalQuery(query_id=f"q{i}", query_type="legal_issue", text=f"规则 {i}") for i in range(5)]
    for decision_model in (AgentDecision, QADecision):
        assert len(decision_model(action="search_web", summary="调查", queries=queries[:4]).queries) == 4
        protocol = ToolCallingAgent(client=None, model_id=None, decision_model=decision_model,
                                   specs={"search_web": INVESTIGATION_TOOLS["search_web"]})
        assert protocol.tools[0]["function"]["parameters"]["properties"]["queries"]["maxItems"] == 4
    with pytest.raises(ValueError):
        AgentDecision(action="search_web", summary="调查", queries=queries)
    with pytest.raises(ValueError):
        QADecision(action="search_web", summary="调查", queries=queries)
    with pytest.raises(ValueError):
        RemediationDecision(action="search_web", summary="复核", queries=[query()])
    decisions = iter([
        *(AgentDecision(action="search_web", summary=f"第{i}次", queries=[query()]) for i in range(1, 7)),
        AgentDecision(action="request_input", summary="收口", question="补充事实"),
    ])
    calls = []
    state = run_agent(
        AgentState(goal="审查"), material="", intake={}, decide=lambda *_: next(decisions),
        search=lambda *_, **_kwargs: [], web_search=lambda *_: calls.append(True) or WebSearchBatch(),
        finalize=lambda *_: {}, checkpoint=lambda *_: None,
    )
    assert len(calls) == 5
    assert state.web_searches == 5
    assert "预算已用尽" in state.steps[5].observation["error"]


def test_web_research_can_reach_fourth_query() -> None:
    calls = []

    class FourthQueryClient:
        def search(self, query, *, max_results):
            calls.append(query)
            return [WebSearchResult(url="https://www.cac.gov.cn/fourth", title="第四个查询结果")] if query == "规则 3" else []

    queries = [RetrievalQuery(query_id=f"q{i}", query_type="legal_issue", text=f"规则 {i}") for i in range(4)]
    findings = WebResearch(client=FourthQueryClient()).search(queries).findings
    assert sorted(calls) == sorted(q.text for q in queries)
    assert findings[0].title == "第四个查询结果"


def test_missing_key_does_not_prevent_bounded_finish(monkeypatch) -> None:
    monkeypatch.setattr("law_agent.review.web_research.load_web_search_api_key", lambda: None)
    decisions = iter([
        AgentDecision(action="search_web", summary="调查", queries=[query()]),
        AgentDecision(action="finish", summary="有边界地收口", draft=_draft()),
    ])
    state = run_agent(
        AgentState(goal="审查"), material="", intake={}, decide=lambda *_: next(decisions),
        search=lambda *_, **_kwargs: [], web_search=lambda *_: build_web_search_client(),
        finalize=lambda *_: {"risk_level": "insufficient_evidence"}, checkpoint=lambda *_: None,
    )
    assert state.steps[0].observation["type"] == "WebSearchUnavailable"
    assert state.status == "completed"
    assert state.result["risk_level"] == "insufficient_evidence"


def test_missing_provider_raises() -> None:
    class FailedClient:
        def search(self, *_args, **_kwargs):
            raise WebSearchUnavailable("unavailable")

    result = WebResearch(client=FailedClient()).search([query()])
    assert result.status == "failed" and result.findings == []
    assert result.query_results[0].error == "unavailable"


def test_populated_first_query_does_not_skip_other_queries() -> None:
    calls = []

    class Client:
        def search(self, query, *, max_results):
            calls.append(query)
            return [WebSearchResult(url=f"https://example.org/{query}/{i}", title=query)
                    for i in range(max_results)]

    queries = [RetrievalQuery(query_id=f"q{i}", query_type="legal_issue", text=f"query{i}") for i in range(4)]
    findings = WebResearch(client=Client()).search(queries, max_results=4).findings
    assert sorted(calls) == [q.text for q in queries]
    assert [item.title for item in findings] == [q.text for q in queries]
    assert len(findings) == 4


def test_round_robin_deduplicates_without_losing_later_query() -> None:
    class Client:
        def search(self, query, *, max_results):
            return [WebSearchResult(url="https://example.org/shared#fragment"),
                    WebSearchResult(url=f"https://example.org/{query}", title=query)]

    queries = [query(), query().model_copy(update={"query_id": "q2", "text": "另一争点"})]
    findings = WebResearch(client=Client()).search(queries).findings
    assert [item.url for item in findings] == [
        "https://example.org/shared#fragment", "https://example.org/另一争点", "https://example.org/数据出境新规",
    ]


def test_official_priority_is_soft_and_other_sources_remain() -> None:
    results = [WebSearchResult(url=f"https://example.org/{i}", title=f"other{i}") for i in range(4)]
    results.insert(1, WebSearchResult(url="https://segg.sh.gov.cn/new", title="官方"))
    findings = WebResearch(client=FakeClient(results)).search([query()]).findings
    assert [item.title for item in findings] == ["官方", "other0", "other1"]
    results.append(results.pop(1))
    findings = WebResearch(client=FakeClient(results)).search([query()]).findings
    assert findings[0].title == "other0"


@pytest.mark.parametrize("url,official", [
    ("https://www.gov.cn/new", True),
    ("https://www.jswx.gov.cn/new", True),
    ("https://segg.sh.gov.cn/new", True),
    ("https://eur-lex.europa.eu/new", True),
    ("https://www.law.tsinghua.edu.cn/new", False),
    ("https://www.cac.gov.cn.evil.org/new", False),
    ("https://www.cac.gov.cn@evil.org/new", False),
    ("https://[invalid", False),
])
def test_official_classification_does_not_grant_clause_qualification(url, official) -> None:
    assert is_official_url(url) is official
    finding = WebFinding(title="title", url=url, is_official=not official)
    assert finding.is_official is official
    assert "can_cite_clause" not in finding.model_dump()


def test_third_party_finding_cannot_be_enqueued_as_official_source() -> None:
    finding = WebFinding(title="律师解读", url="https://example.org/analysis", excerpt="解读")
    decisions = iter([
        AgentDecision(action="search_web", summary="发现", queries=[query()]),
        AgentDecision(action="queue_enrichment", summary="尝试入库", enrichment_urls=[finding.url]),
        AgentDecision(action="request_input", summary="结束", question="补充事实"),
    ])
    queued = []
    state = run_agent(
        AgentState(goal="审查"), material="", intake={}, decide=lambda *_: next(decisions),
        search=lambda *_, **_kwargs: [], web_search=lambda *_: WebSearchBatch(findings=[finding]), on_web_findings=queued.extend,
        finalize=lambda *_: {}, checkpoint=lambda *_: None,
    )
    assert not queued
    assert state.evidence == []
    assert "官方" in state.steps[1].observation["error"]


def test_partial_provider_failure_preserves_successful_query() -> None:
    class Client:
        def search(self, query, *, max_results):
            if query == "失败":
                raise WebSearchUnavailable("timeout")
            return [WebSearchResult(url="https://example.org/result", title="可用线索")]

    queries = [query().model_copy(update={"text": "失败"}), query()]
    assert WebResearch(client=Client()).search(queries).findings[0].title == "可用线索"


def test_query_overflow_is_reported_instead_of_silently_truncated() -> None:
    with pytest.raises(ValueError):
        WebResearch(client=FakeClient([])).search([query()] * 5)


@pytest.mark.parametrize("failure", [httpx.ConnectError("tls failure"), httpx.ReadTimeout("timeout")])
def test_exa_transport_failure_is_explicit_without_retry(failure) -> None:
    with patch("law_agent.review.web_research.httpx.post", side_effect=failure) as post, pytest.raises(
        WebSearchUnavailable, match="Exa 搜索请求失败",
    ):
        ExaSearchClient(api_key="test").search("新规", max_results=3)
    assert post.call_count == 1


@pytest.mark.parametrize("query_count,max_results", [(1, 1), (4, 1), (4, 3), (1, 8), (4, 8)])
def test_result_count_is_independent_of_query_count(query_count, max_results) -> None:
    calls = []

    class Client:
        def search(self, query, *, max_results):
            calls.append((query, max_results))
            return [WebSearchResult(url=f"https://example.org/{query}/{i}", text="x" * 2000)
                    for i in range(max_results)]

    queries = [query().model_copy(update={"query_id": f"q{i}", "text": f"query{i}"}) for i in range(query_count)]
    findings = WebResearch(client=Client()).search(queries, max_results=max_results).findings
    assert len(calls) == query_count
    assert len(findings) == max_results
    assert len({item.url for item in findings}) == max_results
    assert sum(len(item.excerpt) for item in findings) <= max_results * 1200
    assert all(count >= max_results for _, count in calls)


def test_default_count_does_not_expand_for_four_queries() -> None:
    class Client:
        def search(self, query, *, max_results):
            return [WebSearchResult(url=f"https://example.org/{query}/{i}") for i in range(max_results)]

    queries = [query().model_copy(update={"text": f"query{i}"}) for i in range(4)]
    assert len(WebResearch(client=Client()).search(queries).findings) == DEFAULT_WEB_RESULTS


def test_available_results_may_be_fewer_than_requested_without_more_searches() -> None:
    calls = []

    class Client:
        def search(self, query, *, max_results):
            calls.append(query)
            return [WebSearchResult(url="https://example.org/shared")]

    assert len(WebResearch(client=Client()).search([query()], max_results=8).findings) == 1
    assert len(calls) == 1


@pytest.mark.parametrize("count", [0, 9, -1, True, 2.5, "4"])
def test_invalid_count_is_rejected_before_search(count) -> None:
    from law_agent.review.qa_agent import QADecision

    for decision_model in (AgentDecision, QADecision):
        with pytest.raises(ValueError):
            decision_model(action="search_web", summary="调查", queries=[query()], max_results=count)
    with pytest.raises(ValueError, match="max_results"):
        WebResearch(client=FakeClient([])).search([query()], max_results=count)


@pytest.mark.parametrize("qa", [False, True])
def test_native_web_tool_exposes_and_accepts_agent_selected_count(qa) -> None:
    from law_agent.review.agent import INVESTIGATION_TOOLS
    from law_agent.review.qa_agent import QADecision
    from law_agent.review.tool_calling import ToolCallingAgent, ToolSession
    from tests.test_review_llm import FakeClient, tool_message

    protocol = ToolCallingAgent(client=FakeClient(outputs=[tool_message(("web", "search_web", {
        "summary": "比较规则与例外", "queries": [query().model_dump(mode="json")], "max_results": 6,
    }))]), model_id="test", decision_model=QADecision if qa else AgentDecision,
        specs={"search_web": INVESTIGATION_TOOLS["search_web"]})
    schema = protocol.tools[0]["function"]["parameters"]["properties"]["max_results"]
    assert schema["default"] == DEFAULT_WEB_RESULTS
    assert schema["minimum"] == 1 and schema["maximum"] == MAX_WEB_RESULTS
    decision = protocol.decide(ToolSession(), prompt="调查", context={"state": {"evidence": []}})
    assert decision.max_results == 6


def test_review_selected_count_reaches_provider_and_observation() -> None:
    from tests.test_review_agent import _tools_with

    calls = []

    class Client:
        def search(self, query, *, max_results):
            calls.append(max_results)
            return [WebSearchResult(url=f"https://example.org/{i}") for i in range(max_results)]

    tools = _tools_with([])
    tools._web_research = WebResearch(client=Client())
    decisions = iter([
        AgentDecision(action="search_web", summary="检索", queries=[query()], max_results=8),
        AgentDecision(action="request_input", summary="暂停", question="补充事实"),
    ])
    state = run_agent(
        AgentState(goal="审查"), material="", intake={}, decide=lambda *_: next(decisions),
        search=lambda *_, **_kwargs: [], web_search=tools.search_web, finalize=lambda *_: {}, checkpoint=lambda *_: None,
    )
    observation = state.steps[0].observation
    assert calls == [8]
    assert observation["max_results"] == observation["returned_count"] == 8
    assert len(observation["findings"]) == 8
    assert state.web_searches == 1


def test_eligible_material_web_urls_deduplicates_canonical_urls():
    from law_agent.review.web_research import eligible_material_web_urls

    findings = [
        WebFinding(title="发布页", url="http://www.gov.cn/guide/"),
        WebFinding(title="发布页锚点", url="https://gov.cn/guide#section"),
    ]
    assert eligible_material_web_urls(findings) == [findings[-1].url]
