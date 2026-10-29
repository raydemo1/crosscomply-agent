from datetime import date
from unittest.mock import patch

import httpx
import pytest

from law_agent.data.schemas import Chunk
from law_agent.review.agent import AgentDecision, AgentState, AgentStep, investigate
from law_agent.review.qa_agent import QAState, source_citations
from law_agent.review.qa_citations import render_qa_citations
from law_agent.review.schemas import RetrievalQuery, ReviewFacts
from law_agent.review.web_research import (
    MAX_EXCERPT_CHARACTERS,
    ExaSearchClient,
    WebFinding,
    WebResearch,
    WebSearchResult,
    WebSearchUnavailable,
    canonical_url,
    is_public_web_url,
)


def query(text="关键条文", query_id="q1"):
    return RetrievalQuery(query_id=query_id, query_type="legal_issue", text=text)


def response(body):
    return httpx.Response(200, json=body, request=httpx.Request("POST", "https://api.exa.ai/search"))


@pytest.mark.parametrize("body", [
    {}, [], {"error": "backend failure"}, {"results": {}},
    {"results": [], "error": "failure"},
    {"results": [None]}, {"results": [{"url": {"bad": "type"}}]},
    {"results": [{"url": "https://example.org/a", "title": []}]},
    {"results": [{"url": "https://example.org/a", "highlights": "wrong type"}]},
    {"results": [{"url": "https://example.org/a", "highlights": [{}]}]},
    {"results": [{"url": "https://example.org/a", "publishedDate": "not-a-date"}]},
    {"results": [{"url": "https://example.org/a", "publishedDate": "2024-02-30"}]},
])
def test_protocol_errors_are_not_empty_searches(body):
    with (
        patch("law_agent.review.web_research.httpx.post", return_value=response(body)),
        pytest.raises(WebSearchUnavailable, match="响应格式错误"),
    ):
        ExaSearchClient(api_key="test").search("test", max_results=3)


def test_target_passage_comes_from_highlights_and_keeps_omission_boundaries():
    body = {"results": [{"url": "https://www.cac.gov.cn/law", "title": "法规",
                         "text": "前言" * 1000,
                         "highlights": ["第七十三条 目标定义", "另一个相关原文段落"],
                         "publishedDate": "2024-01-01T10:20:30Z"}]}
    with patch("law_agent.review.web_research.httpx.post", return_value=response(body)) as post:
        finding = WebResearch(client=ExaSearchClient(api_key="test")).search([query()]).findings[0]
    assert finding.excerpt == "第七十三条 目标定义\n\n[…]\n\n另一个相关原文段落"
    assert finding.excerpt_type == "query_highlights"
    assert finding.excerpt_query_id == "q1"
    assert finding.published_date == "2024-01-01"
    assert post.call_count == 1
    assert post.call_args.kwargs["json"]["contents"] == {
        "highlights": {"query": "关键条文", "maxCharacters": MAX_EXCERPT_CHARACTERS},
    }


def test_provider_oversized_highlights_are_bounded_and_marked():
    body = {"results": [{"url": "https://example.org/a", "title": "t" * 5000,
                         "highlights": ["x" * 2000]}]}
    with patch("law_agent.review.web_research.httpx.post", return_value=response(body)):
        finding = WebResearch(client=ExaSearchClient(api_key="test")).search([query()]).findings[0]
    assert len(finding.excerpt) == MAX_EXCERPT_CHARACTERS
    assert finding.excerpt_truncated is True
    assert len(finding.title) == 500


def test_empty_highlights_are_visible_without_text_fallback_or_extra_requests():
    body = {"results": [{"url": "https://example.org/a", "text": "unrequested text"}]}
    with patch("law_agent.review.web_research.httpx.post", return_value=response(body)) as post:
        finding = WebResearch(client=ExaSearchClient(api_key="test")).search([query()]).findings[0]
    assert finding.excerpt == "" and finding.excerpt_type == "none"
    assert post.call_count == 1


def test_partial_query_failure_reaches_agent_with_query_identity():
    class Client:
        def search(self, text, *, max_results):
            if text == "失败":
                raise WebSearchUnavailable("timeout")
            return [WebSearchResult(url="https://www.cac.gov.cn/new", text="原文")]

    research = WebResearch(client=Client())
    observation = investigate(
        AgentState(goal="test"),
        AgentDecision(action="search_web", summary="调查", queries=[query("失败"), query("成功", "q2")]),
        search=lambda *_, **_kwargs: [], search_navigation=None, read_evidence=None, web_search=lambda qs, facts, count: research.search(qs, facts, max_results=count),
        checkpoint=lambda _: None,
    )
    assert observation["status"] == "partial"
    failed, succeeded = observation["query_results"]
    assert failed["query_id"] == "q1" and failed["error"] == "timeout"
    assert failed["status"] == "error" and failed["returned_urls"] == []
    assert succeeded["query_id"] == "q2" and succeeded["status"] == "success"
    assert succeeded["returned_urls"] == ["https://www.cac.gov.cn/new"]
    assert observation["findings"][0]["excerpt_query_id"] == "q2"


def test_all_failed_queries_preserve_each_error_and_valid_empty_is_distinct():
    class Client:
        def search(self, text, *, max_results):
            if text != "empty":
                raise WebSearchUnavailable(text)
            return []

    research = WebResearch(client=Client())
    failed = research.search([query("first"), query("second", "q2")])
    assert failed.status == "failed" and failed.findings == []
    assert [q.error for q in failed.query_results] == ["first", "second"]
    empty = research.search([query("empty")])
    assert empty.status == "complete" and empty.query_results[0].status == "empty"


@pytest.mark.parametrize("url", [
    "http://localhost/a", "http://host.local/a", "http://host.internal/a", "http://127.0.0.1/a",
    "http://10.0.0.1/a", "http://169.254.169.254/a", "http://[::1]/a",
    "http://[::ffff:127.0.0.1]/a", "http://224.0.0.1/a", "http://2130706433/a",
    "http://127.1/a", "https://example.org:invalid/a", "https://example.org:65536/a",
    "https://example.org:0/a", "https://example.org\n.evil.com/a",
])
def test_only_well_formed_public_web_urls_are_admitted(url):
    assert not is_public_web_url(url)


def test_url_identity_preserves_ports_and_document_parameters():
    assert canonical_url("https://example.org:8443/a") != canonical_url("https://example.org/a")
    assert canonical_url("http://www.example.org:80/a/#fragment") == canonical_url("https://example.org/a")
    assert canonical_url("https://example.org/a?id=1&utm_source=x&gclid=y") == canonical_url("https://example.org/a?id=1")
    assert canonical_url("https://example.org/a?id=1") != canonical_url("https://example.org/a?id=2")
    assert canonical_url("https://[2606:4700:4700::1111]:8443/a") == "https://[2606:4700:4700::1111]:8443/a"


def test_existing_source_whitespace_or_disjoint_passages_do_not_trigger_refresh():
    class Client:
        def search(self, text, *, max_results):
            return [WebSearchResult(url="https://www.cac.gov.cn/a", text="第一条 条件\n\n[…]\n\n第三条 例外")]

    chunk = Chunk(chunk_id="c", doc_id="d", source_id="s", title="t", chunk_index=0,
                  text="第一条\n条件 第二条 其他 第三条\n例外", char_count=20,
                  source_url="https://www.cac.gov.cn/a")
    finding = WebResearch(client=Client(), corpus_chunks=[chunk]).search([query()]).findings[0]
    assert finding.known_source_id == "s" and finding.refresh_needed is False


def test_web_date_warning_survives_citation_and_recomputes_for_changed_answer_date():
    finding = WebFinding(title="later", url="https://www.cac.gov.cn/a", excerpt="原文",
                         published_date="2024-03-22", excerpt_type="query_highlights", excerpt_truncated=True)
    state = QAState(goal="历史", facts=ReviewFacts(as_of_date=date(2023, 12, 31)),
                    steps=[AgentStep(number=1, action="search_web", summary="test",
                                     observation={"findings": [finding.model_dump(mode="json")]})])
    citation = source_citations(state)[0]
    assert citation["published_date"] == "2024-03-22" and citation["published_after_as_of"] is True
    assert citation["excerpt_type"] == "query_highlights" and citation["excerpt_truncated"] is True
    answer, used = render_qa_citations("比较" + citation["citation_marker"], [citation])
    assert "发布晚于判断时点，仅供追溯或比较" in answer
    assert used[0]["kind"] == "web" and used[0]["can_cite_clause"] is False
    state.facts.as_of_date = date(2025, 1, 1)
    later = source_citations(state)[0]
    assert later["published_after_as_of"] is False
    assert later["citation_marker"] == citation["citation_marker"]
    assert "available_at_answer_date" not in later
