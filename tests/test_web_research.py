"""Web observations cannot become formal clause evidence."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from law_agent.data.schemas import Chunk
from law_agent.review.agent import AgentDecision, AgentState, run_agent
from law_agent.review.schemas import RetrievalHit, RetrievalQuery
from law_agent.review.web_research import (
    ExaSearchClient,
    WebFinding,
    WebResearch,
    WebSearchResult,
    WebSearchUnavailable,
    is_trusted_official_url,
)


def query() -> RetrievalQuery:
    return RetrievalQuery(query_id="q1", query_type="legal_issue", text="数据出境新规")


class FakeClient:
    def __init__(self, results: list[WebSearchResult]):
        self.results = results

    def search(self, _query: str, *, max_results: int) -> list[WebSearchResult]:
        return self.results[:max_results]


def test_official_content_is_visible_but_never_joins_evidence() -> None:
    finding = WebFinding(title="新规", url="https://www.cac.gov.cn/new.html", excerpt="新的办理要求", status="read")
    decisions = iter([
        AgentDecision(action="search_web", summary="查新规", queries=[query()]),
        AgentDecision(action="queue_enrichment", summary="补充相关官方来源", enrichment_urls=[finding.url]),
        AgentDecision(action="request_input", summary="等待", question="请补充事实"),
    ])
    enqueued: list[WebFinding] = []
    state = run_agent(
        AgentState(goal="审查"), material="", rule={},
        decide=lambda *_: next(decisions), search=lambda *_: [],
        web_search=lambda *_: [finding], on_web_findings=enqueued.extend,
        finalize=lambda *_: {}, checkpoint=lambda *_: None,
    )
    assert state.evidence == []
    assert state.web_findings == [finding]
    assert enqueued == [finding]
    assert state.steps[0].observation["findings"][0]["excerpt"] == "新的办理要求"
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


def test_resumed_agent_discards_legacy_web_hits() -> None:
    old = RetrievalHit(
        chunk_id="web:old", doc_id="old", source_id="old", title="旧网页",
        text="未经治理的条款", score=1.0, rank=0, retriever="web",
        citation_role="primary_legal_basis", can_cite_clause=True,
        source_url="https://www.cac.gov.cn/old",
    )
    state = run_agent(
        AgentState(goal="审查", evidence=[old]), material="", rule={},
        decide=lambda *_: AgentDecision(action="request_input", summary="等待", question="补充"),
        search=lambda *_: [], finalize=lambda *_: {}, checkpoint=lambda *_: None,
    )
    assert state.evidence == []


def test_official_host_is_checked_again_after_provider_returns() -> None:
    research = WebResearch(client=FakeClient([
        WebSearchResult(url="https://www.cac.gov.cn/new", text="正文"),
        WebSearchResult(url="https://www.cac.gov.cn.evil.com/new", text="伪造正文"),
        WebSearchResult(url="https://www.cac.gov.cn/new", text="重复"),
    ]))
    findings = research.search([query()])
    assert len(findings) == 1
    assert findings[0].status == "read"
    assert not is_trusted_official_url("https://www.cac.gov.cn.evil.com/new")


def test_known_official_url_is_flagged_when_publication_date_is_newer() -> None:
    chunk = Chunk(
        chunk_id="old:1", doc_id="old", source_id="old", title="旧版",
        text="旧条款", chunk_index=0, source_url="https://www.cac.gov.cn/new",
        publish_date="2024-01-01", char_count=3,
    )
    findings = WebResearch(
        client=FakeClient([WebSearchResult(
            url="https://www.cac.gov.cn/new", title="新版",
            text="新条款", published_date="2026-09-01",
        )]),
        corpus_chunks=[chunk],
    ).search([query()])
    assert findings[0].known_source_id == "old"
    assert findings[0].refresh_needed


def test_exa_requests_bounded_text_without_summary_llm() -> None:
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return json.dumps({"results": [{
                "url": "https://www.cac.gov.cn/new", "title": "新规",
                "text": "正文", "publishedDate": "2026-09-01",
            }]}).encode()

    with patch("urllib.request.urlopen", return_value=Response()) as open_url:
        results = ExaSearchClient(api_key="test").search("新规", max_results=2)
    payload = json.loads(open_url.call_args.args[0].data)
    assert payload["contents"]["text"]["maxCharacters"] == 6000
    assert "summary" not in payload["contents"]
    assert results[0].text == "正文"
    assert results[0].published_date == "2026-09-01"


def test_missing_provider_raises() -> None:
    class FailedClient:
        def search(self, *_args, **_kwargs):
            raise WebSearchUnavailable("unavailable")

    with pytest.raises(WebSearchUnavailable):
        WebResearch(client=FailedClient()).search([query()])
