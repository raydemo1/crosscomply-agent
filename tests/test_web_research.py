"""Web observations cannot become formal clause evidence."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from law_agent.data.schemas import Chunk
from law_agent.review.agent import AgentDecision, AgentState, run_agent
from law_agent.review.remediation import RemediationDecision
from law_agent.review.schemas import RetrievalHit, RetrievalQuery
from law_agent.review.web_research import (
    TRUSTED_SEARCH_DOMAINS,
    ExaSearchClient,
    WebFinding,
    WebResearch,
    WebSearchResult,
    WebSearchUnavailable,
    build_web_search_client,
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
        AgentState(goal="审查"), material="", rule={},
        decide=decide, search=lambda *_: [],
        web_search=lambda *_: [finding], on_web_findings=enqueued.extend,
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
        AgentState(goal="审查"), material="", rule={},
        decide=lambda *_: next(decisions), search=lambda *_: [],
        web_search=lambda *_: [finding], on_web_findings=enqueued.extend,
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
    ).search([query()])
    assert findings[0].known_source_id == "old"
    assert findings[0].excerpt == "搜索摘录"
    assert set(findings[0].model_dump()) == {"title", "url", "excerpt", "known_source_id"}

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
        AgentState(goal="审查"), material="", rule={}, decide=lambda *_: next(decisions),
        search=lambda *_: [hit], web_search=lambda *_: findings,
        finalize=lambda *_: {}, checkpoint=lambda *_: None,
    )
    assert state.evidence == [hit]
    assert state.queries == [query()]
    assert state.steps[0].observation["findings"][0]["known_source_id"] == "old"


def test_exa_requests_bounded_text_without_summary_llm() -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"results": [{
                "url": "https://www.cac.gov.cn/new", "title": "新规",
                "text": "正文",
            }]}).encode()

    with patch("urllib.request.urlopen", return_value=Response()) as open_url:
        results = ExaSearchClient(api_key="test").search("新规", max_results=2)
    payload = json.loads(open_url.call_args.args[0].data)
    assert payload["contents"]["text"]["maxCharacters"] == 1200
    assert "summary" not in payload["contents"]
    assert payload["includeDomains"] == list(TRUSTED_SEARCH_DOMAINS)
    assert len(payload["includeDomains"]) == 11
    assert "www.gov.cn" not in payload["includeDomains"]
    assert results[0].text == "正文"


def test_no_excerpt_does_not_trigger_page_fetch() -> None:
    findings = WebResearch(client=FakeClient([
        WebSearchResult(url="https://www.cac.gov.cn/new", title="新规"),
    ])).search([query()])
    assert findings == [WebFinding(url="https://www.cac.gov.cn/new", title="新规")]


def test_web_action_limit_is_two_and_query_limit_is_three() -> None:
    queries = [RetrievalQuery(query_id=f"q{i}", query_type="legal_issue", text=f"规则 {i}") for i in range(4)]
    with pytest.raises(ValueError, match="1-3"):
        AgentDecision(action="search_web", summary="调查", queries=queries)
    with pytest.raises(ValueError):
        RemediationDecision(action="search_web", summary="复核", queries=[query()])
    decisions = iter([
        AgentDecision(action="search_web", summary="第一次", queries=[query()]),
        AgentDecision(action="search_web", summary="第二次", queries=[query()]),
        AgentDecision(action="search_web", summary="第三次", queries=[query()]),
        AgentDecision(action="request_input", summary="收口", question="补充事实"),
    ])
    calls = []
    state = run_agent(
        AgentState(goal="审查"), material="", rule={}, decide=lambda *_: next(decisions),
        search=lambda *_: [], web_search=lambda *_: calls.append(True) or [],
        finalize=lambda *_: {}, checkpoint=lambda *_: None,
    )
    assert len(calls) == 2
    assert state.web_searches == 2
    assert "预算已用尽" in state.steps[2].observation["error"]


def test_missing_key_does_not_prevent_bounded_finish(monkeypatch) -> None:
    monkeypatch.setattr("law_agent.review.web_research.load_web_search_api_key", lambda: None)
    decisions = iter([
        AgentDecision(action="search_web", summary="调查", queries=[query()]),
        AgentDecision(action="finish", summary="有边界地收口", draft=_draft()),
    ])
    state = run_agent(
        AgentState(goal="审查"), material="", rule={}, decide=lambda *_: next(decisions),
        search=lambda *_: [], web_search=lambda *_: build_web_search_client(),
        finalize=lambda *_: {"risk_level": "insufficient_evidence"}, checkpoint=lambda *_: None,
    )
    assert state.steps[0].observation["type"] == "WebSearchUnavailable"
    assert state.status == "completed"
    assert state.result["risk_level"] == "insufficient_evidence"


def test_missing_provider_raises() -> None:
    class FailedClient:
        def search(self, *_args, **_kwargs):
            raise WebSearchUnavailable("unavailable")

    with pytest.raises(WebSearchUnavailable):
        WebResearch(client=FailedClient()).search([query()])
