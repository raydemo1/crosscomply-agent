"""Behavioral tests for the Agent's bounded public-web investigation.

The web tool is intentionally narrow, so these tests pin the boundary instead
of the provider: what is read, what may become evidence, which sources keep
their citation privileges, and how a failing provider is contained.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Self

import pytest

from law_agent.data.fetchers.generic import FetchResult
from law_agent.data.schemas import Chunk, SourceRecord
from law_agent.review.agent import AgentDecision, AgentState, run_agent
from law_agent.review.remediation import (
    RemediationAssessmentDraft,
    RemediationDecision,
    RereviewPacket,
    execute_rereview,
)
from law_agent.review.result_builder import LLMReviewResultDraft
from law_agent.review.schemas import RetrievalHit, RetrievalQuery
from law_agent.review.web_research import (
    TRUSTED_SEARCH_DOMAINS,
    ExaSearchClient,
    WebResearch,
    WebSearchResult,
    WebSearchUnavailable,
    _file_format_for,
)

KNOWN_SOURCE_ID = "cac_cross_border_data_flow_rules_2024"
KNOWN_URL = "https://www.cac.gov.cn/2024-03/22/c_1712776611775634.htm"
NEW_URL = "https://www.mnr.gov.cn/dt/2026-01/05/content_9999.htm"
# The governed corpus holds one document republished on a news site, so a page
# read there must keep the identity the corpus already gave it.
SZNEWS_SOURCE_ID = "shenzhen_data_regulation_2021"
SZNEWS_URL = "http://www.sznews.com/zhuanti/content/2021-07/07/content_24368291.htm"

_KNOWN_PAGE = """
<html><body>
<h1>促进和规范数据跨境流动规定</h1>
<p>第三条 数据出境安全评估、个人信息出境标准合同、个人信息保护认证等数据出境活动，应当符合本规定。</p>
<p>第四条 数据处理者向境外提供个人信息，应当按照相关规定履行相应手续，并留存必要记录。</p>
</body></html>
"""

_NEW_PAGE = """
<html><body>
<h1>关于数据出境有关事项的通知</h1>
<p>为规范数据出境活动，现将数据出境安全评估有关事项通知如下，请遵照执行。</p>
<p>数据处理者开展数据出境活动，应当按照规定履行安全评估手续并保存相关材料。</p>
</body></html>
"""

_DRAFT = LLMReviewResultDraft(
    risk_level="insufficient_evidence",
    decision_summary=(
        "当前材料和法源证据仍不足以形成正式风险结论，需要明确标示调查边界、"
        "尚未核实事项以及后续补充要求。"
    ),
    conclusion="现有证据不足，暂不形成正式合规路径结论。",
    trigger_reasons=["证据不足"],
    missing_information=["境外接收方所在地区"],
    recommended_actions=["补充接收方信息"],
    risk_boundaries=["未覆盖地方和行业特别规则"],
)


def _query(text: str = "数据出境安全评估") -> RetrievalQuery:
    return RetrievalQuery(query_id="q1", query_type="legal_issue", text=text)


def _web_decision(text: str = "数据出境安全评估最新规定") -> AgentDecision:
    return AgentDecision(
        action="search_web",
        summary="正在核实最新官方规则",
        queries=[_query(text)],
    )


def _finish_decision() -> AgentDecision:
    return AgentDecision(action="finish", summary="交付当前调查结果", draft=_DRAFT)


def _corpus_chunk() -> Chunk:
    """One governed chunk, so the corpus can recognize the official URL."""

    return Chunk(
        chunk_id=f"{KNOWN_SOURCE_ID}:0000",
        doc_id=KNOWN_SOURCE_ID,
        source_id=KNOWN_SOURCE_ID,
        title="促进和规范数据跨境流动规定",
        text="第三条 数据出境安全评估、个人信息出境标准合同、个人信息保护认证等数据出境活动，应当符合本规定。",
        chunk_index=0,
        doc_type="law",
        article_no="第三条",
        citation_label="促进和规范数据跨境流动规定 第三条",
        citation_role="primary_legal_basis",
        can_cite_clause=True,
        authority="ministry_policy",
        law_status="effective",
        source_url=KNOWN_URL,
        char_count=48,
    )


def _sznews_chunk() -> Chunk:
    """A governed source that lives on a news site rather than a government portal."""

    return Chunk(
        chunk_id=f"{SZNEWS_SOURCE_ID}:0000",
        doc_id=SZNEWS_SOURCE_ID,
        source_id=SZNEWS_SOURCE_ID,
        title="深圳经济特区数据条例",
        text="数据处理者开展数据处理活动，应当遵守法律、法规，履行数据安全保护义务。",
        chunk_index=0,
        doc_type="law",
        citation_role="primary_legal_basis",
        can_cite_clause=True,
        authority="ministry_policy",
        law_status="effective",
        source_url=SZNEWS_URL,
        char_count=32,
    )


class FakeSearchClient:
    """Test double for the search-provider boundary; URLs only, like the real one."""

    def __init__(
        self,
        results: list[WebSearchResult] | None = None,
        *,
        errors: dict[str, str] | None = None,
    ) -> None:
        self._results = list(results or [])
        self._errors = dict(errors or {})
        self.queries: list[str] = []

    def search(self, query: str, *, max_results: int) -> list[WebSearchResult]:
        self.queries.append(query)
        if query in self._errors:
            raise WebSearchUnavailable(self._errors[query])
        return self._results[:max_results]


class FakeFetcher:
    """Writes a page body to disk, or reports an explicit fetch failure."""

    def __init__(self, pages: dict[str, str], *, fail: bool = False) -> None:
        self._pages = dict(pages)
        self._fail = fail
        self.calls: list[str] = []

    def __call__(
        self, record: SourceRecord, output_dir: Path, *, timeout_seconds: int
    ) -> FetchResult:
        self.calls.append(record.source_url)
        path = output_dir / f"{record.source_id}.html"
        if self._fail or record.source_url not in self._pages:
            return FetchResult(
                source_id=record.source_id, path=path, ok=False, error="page unavailable"
            )
        path.write_text(self._pages[record.source_url], encoding="utf-8")
        return FetchResult(source_id=record.source_id, path=path, ok=True, sha256="deadbeef")


def _research(
    client: FakeSearchClient, fetcher: FakeFetcher, *, max_pages: int = 3
) -> WebResearch:
    return WebResearch(
        client=client,
        corpus_chunks=[_corpus_chunk()],
        max_pages=max_pages,
        fetch=fetcher,
    )


def _web_hit() -> RetrievalHit:
    return RetrievalHit(
        chunk_id=f"web:{KNOWN_SOURCE_ID}:0000",
        doc_id=KNOWN_SOURCE_ID,
        source_id=KNOWN_SOURCE_ID,
        title="促进和规范数据跨境流动规定",
        text="第三条 数据出境安全评估……",
        score=0.5,
        rank=0,
        retriever="web",
        citation_role="primary_legal_basis",
        can_cite_clause=True,
        source_url=KNOWN_URL,
        article_no="第三条",
    )


# ---------------------------------------------------------------------------
# Agent-level behaviour
# ---------------------------------------------------------------------------


def test_sufficient_local_evidence_never_triggers_a_web_search() -> None:
    decisions = iter(
        [
            AgentDecision(
                action="search_evidence",
                summary="检索受控法源",
                queries=[_query()],
            ),
            _finish_decision(),
        ]
    )
    web_calls: list[str] = []

    state = run_agent(
        AgentState(goal="审查数据出境"),
        material="已冻结材料",
        rule={},
        decide=lambda _state, _rule: next(decisions),
        search=lambda _queries, _facts: [],
        web_search=lambda queries, _facts: web_calls.extend(q.text for q in queries) or [],
        finalize=lambda _draft, _state: {"ok": True},
        checkpoint=lambda _state: None,
    )

    assert web_calls == []
    assert state.web_searches == 0
    assert [step.action for step in state.steps] == ["search_evidence", "finish"]


def test_agent_may_choose_search_web_and_its_hits_join_the_same_evidence() -> None:
    decisions = iter([_web_decision(), _finish_decision()])
    hit = _web_hit()

    state = run_agent(
        AgentState(goal="审查数据出境"),
        material="已冻结材料",
        rule={},
        decide=lambda _state, _rule: next(decisions),
        search=lambda _queries, _facts: [],
        web_search=lambda _queries, _facts: [hit],
        finalize=lambda _draft, _state: {"ok": True},
        checkpoint=lambda _state: None,
    )

    assert state.web_searches == 1
    assert [item.chunk_id for item in state.evidence] == [hit.chunk_id]
    assert state.steps[0].action == "search_web"
    assert state.steps[0].observation["source_urls"] == [KNOWN_URL]
    assert state.steps[0].observation["citable_count"] == 1


def test_web_search_budget_stops_after_two_searches() -> None:
    decisions = iter([_web_decision(), _web_decision(), _web_decision(), _finish_decision()])
    calls: list[str] = []

    state = run_agent(
        AgentState(goal="审查数据出境"),
        material="已冻结材料",
        rule={},
        decide=lambda _state, _rule: next(decisions),
        search=lambda _queries, _facts: [],
        web_search=lambda queries, _facts: calls.extend(q.text for q in queries) or [],
        finalize=lambda _draft, _state: {"ok": True},
        checkpoint=lambda _state: None,
    )

    assert len(calls) == 2
    assert state.web_searches == 2
    errors = [step.observation.get("error", "") for step in state.steps]
    assert any("预算" in error for error in errors)
    assert state.status == "completed"


def test_a_deployment_without_web_access_still_runs_reviews() -> None:
    """Without a configured provider the Agent is told so, and keeps working."""

    decisions = iter([_web_decision(), _finish_decision()])

    state = run_agent(
        AgentState(goal="审查数据出境"),
        material="已冻结材料",
        rule={},
        decide=lambda _state, _rule: next(decisions),
        search=lambda _queries, _facts: [],
        finalize=lambda _draft, _state: {"ok": True},
        checkpoint=lambda _state: None,
    )

    assert state.status == "completed"
    assert "未启用 Web 调查能力" in state.steps[0].observation["error"]


def test_a_failing_web_provider_does_not_break_the_review_task() -> None:
    decisions = iter(
        [
            _web_decision(),
            AgentDecision(
                action="search_evidence",
                summary="退回受控法源继续检索",
                queries=[_query()],
            ),
            _finish_decision(),
        ]
    )
    searched: list[str] = []

    def failing_web(_queries: list[RetrievalQuery], _facts: object) -> list[RetrievalHit]:
        raise WebSearchUnavailable("Web 搜索请求失败：连接超时")

    state = run_agent(
        AgentState(goal="审查数据出境"),
        material="已冻结材料",
        rule={},
        decide=lambda _state, _rule: next(decisions),
        search=lambda queries, _facts: searched.extend(q.text for q in queries) or [],
        web_search=failing_web,
        finalize=lambda _draft, _state: {"ok": True},
        checkpoint=lambda _state: None,
    )

    assert state.status == "completed"
    assert "Web 搜索请求失败" in state.steps[0].observation["error"]
    assert searched == ["数据出境安全评估"]
    assert [step.action for step in state.steps] == [
        "search_web", "search_evidence", "finish",
    ]


def test_remediation_agent_can_use_the_same_web_tool() -> None:
    decisions = iter(
        [
            RemediationDecision(
                action="search_web",
                summary="正在核实最新官方规则",
                queries=[_query("数据出境标准合同备案最新要求")],
            ),
            RemediationDecision(
                action="finish",
                summary="提交复核判断",
                draft=RemediationAssessmentDraft(
                    status="not_resolved",
                    summary="整改尚未完成，仍需完成标准合同备案。",
                    remaining_gaps=["尚未提交个人信息出境标准合同备案"],
                    next_request="请补充标准合同备案材料后重新提交。",
                ),
            ),
        ]
    )
    calls: list[str] = []

    state = execute_rereview(
        packet=RereviewPacket(text="复核材料包"),
        goal="复核整改是否完成",
        state=None,
        model_id="test-model",
        chunks_path=Path("unused-chunks.jsonl"),
        decide=lambda _state, _rule: next(decisions),
        search=lambda _queries, _facts: [],
        web_search=lambda queries, _facts: calls.extend(q.text for q in queries) or [],
    )

    assert calls == ["数据出境标准合同备案最新要求"]
    assert state.web_searches == 1
    assert state.status == "completed"


# ---------------------------------------------------------------------------
# Web research behaviour
# ---------------------------------------------------------------------------


def test_results_that_are_not_urls_are_dropped_without_reordering() -> None:
    """The search pool is settled by the provider; here only URLs are checked."""

    fetcher = FakeFetcher({KNOWN_URL: _KNOWN_PAGE})
    client = FakeSearchClient(
        [
            WebSearchResult(url="解读与评论", title="不是链接"),
            WebSearchResult(url=KNOWN_URL, title="规定"),
        ]
    )

    hits = _research(client, fetcher).search([_query()])

    assert fetcher.calls == [KNOWN_URL]
    assert {hit.source_id for hit in hits} == {KNOWN_SOURCE_ID}


def test_a_governed_source_outside_a_government_portal_keeps_its_own_identity() -> None:
    """Citation rights follow the governed URL, not the domain hosting it.

    The page is matched back to the source the corpus already holds, so its
    passages carry that source's citation role rather than being minted as a
    fresh, auxiliary ``web_<hash>`` discovery.
    """

    fetcher = FakeFetcher({SZNEWS_URL: _KNOWN_PAGE})
    client = FakeSearchClient(
        [WebSearchResult(url=SZNEWS_URL, title="深圳经济特区数据条例")]
    )
    research = WebResearch(
        client=client,
        corpus_chunks=[_corpus_chunk(), _sznews_chunk()],
        fetch=fetcher,
    )

    hits = research.search([_query()])

    assert hits
    assert {hit.source_id for hit in hits} == {SZNEWS_SOURCE_ID}
    assert all(hit.citation_role == "conditional_local_basis" for hit in hits)
    assert all(hit.can_cite_clause is False for hit in hits)


def test_the_discovery_allowlist_stays_closed() -> None:
    """Widening the search pool must be a deliberate edit, not a silent drift."""

    assert set(TRUSTED_SEARCH_DOMAINS) == {
        "flk.npc.gov.cn",
        "sousuo.www.gov.cn",
        "xzfg.moj.gov.cn",
        "www.moj.gov.cn",
        "www.court.gov.cn",
        "rmfyalk.court.gov.cn",
        "www.spp.gov.cn",
        "www.cac.gov.cn",
        "www.miit.gov.cn",
        "openstd.samr.gov.cn",
        "www.mnr.gov.cn",
    }
    assert len(TRUSTED_SEARCH_DOMAINS) == 11
    # A bare ``www.`` entry of an apex domain collapses to the whole of gov.cn,
    # which is why State Council is absent from the pool rather than present.
    assert "www.gov.cn" not in TRUSTED_SEARCH_DOMAINS


def test_a_document_url_keeps_its_document_format() -> None:
    """Labelling a PDF as HTML turns its bytes into thousands of noise chars."""

    assert _file_format_for("https://www.tc260.org.cn/upload/2024/a.pdf") == "pdf"
    assert _file_format_for("https://example.gov.cn/notice.docx") == "docx"
    assert _file_format_for("https://www.cac.gov.cn/2024-03/22/c_1.htm") == "html"
    assert _file_format_for("https://example.gov.cn/notes.txt") == "txt"


def test_search_snippet_is_never_turned_into_evidence() -> None:
    """A result whose body cannot be read contributes nothing, not its title."""

    snippet = "第三条 数据处理者向境外提供个人信息应当履行安全评估手续"
    fetcher = FakeFetcher({}, fail=True)
    client = FakeSearchClient([WebSearchResult(url=KNOWN_URL, title=snippet)])

    hits = _research(client, fetcher).search([_query()])

    assert hits == []
    assert fetcher.calls == [KNOWN_URL]


def test_known_official_url_keeps_its_governed_identity_and_citation_rights() -> None:
    fetcher = FakeFetcher({KNOWN_URL: _KNOWN_PAGE})
    client = FakeSearchClient(
        [WebSearchResult(url=KNOWN_URL, title="促进和规范数据跨境流动规定")]
    )

    hits = _research(client, fetcher).search([_query()])

    assert hits
    assert {hit.source_id for hit in hits} == {KNOWN_SOURCE_ID}
    assert all(hit.retriever == "web" for hit in hits)
    third = next(hit for hit in hits if hit.article_no == "第三条")
    assert third.citation_role == "primary_legal_basis"
    assert third.can_cite_clause is True
    assert third.chunk_id == f"web:{KNOWN_SOURCE_ID}:0000"
    assert third.authority == "ministry_policy"


def test_newly_discovered_official_url_stays_auxiliary_and_uncitable() -> None:
    fetcher = FakeFetcher({NEW_URL: _NEW_PAGE})
    client = FakeSearchClient([WebSearchResult(url=NEW_URL, title="关于数据出境有关事项的通知")])

    hits = _research(client, fetcher).search([_query()])

    assert hits
    assert all(hit.source_id.startswith("web_") for hit in hits)
    assert all(hit.citation_role == "interpretation_auxiliary" for hit in hits)
    assert all(hit.can_cite_clause is False for hit in hits)
    assert {hit.source_url for hit in hits} == {NEW_URL}


def test_web_chunk_ids_cannot_collide_with_governed_corpus_chunk_ids() -> None:
    """A re-read page numbers its own chunks from zero, like the corpus does."""

    fetcher = FakeFetcher({KNOWN_URL: _KNOWN_PAGE})
    client = FakeSearchClient([WebSearchResult(url=KNOWN_URL, title="规定")])

    hits = _research(client, fetcher).search([_query()])

    assert hits
    assert all(hit.chunk_id not in {_corpus_chunk().chunk_id} for hit in hits)
    assert all(hit.chunk_id.startswith("web:") for hit in hits)


def test_provider_failure_is_reported_only_when_nothing_could_be_read() -> None:
    client = FakeSearchClient([], errors={"数据出境安全评估": "连接超时"})

    with pytest.raises(WebSearchUnavailable):
        _research(client, FakeFetcher({})).search([_query()])


def test_one_failing_query_does_not_discard_pages_read_from_the_others() -> None:
    fetcher = FakeFetcher({KNOWN_URL: _KNOWN_PAGE})
    client = FakeSearchClient(
        [WebSearchResult(url=KNOWN_URL, title="规定")],
        errors={"无法检索的查询": "连接超时"},
    )

    hits = _research(client, fetcher).search([_query("无法检索的查询"), _query()])

    assert hits
    assert {hit.source_id for hit in hits} == {KNOWN_SOURCE_ID}


def test_only_the_page_budget_is_read_even_when_more_official_results_return() -> None:
    urls = [f"https://www.cac.gov.cn/page-{index}.htm" for index in range(5)]
    fetcher = FakeFetcher({url: _KNOWN_PAGE for url in urls})
    client = FakeSearchClient([WebSearchResult(url=url, title="规定") for url in urls])

    hits = _research(client, fetcher, max_pages=3).search([_query()])

    assert len(fetcher.calls) == 3
    assert len({hit.source_url for hit in hits}) == 3


def test_a_page_with_no_query_overlap_contributes_no_evidence() -> None:
    """Navigation and footer boilerplate must not spend the page budget."""

    page = """
    <html><body>
    <h1>网站导航</h1>
    <p>设为首页 加入收藏 手机版 繁体版 无障碍浏览 网站地图 联系我们 版权声明</p>
    </body></html>
    """
    fetcher = FakeFetcher({NEW_URL: page})
    client = FakeSearchClient([WebSearchResult(url=NEW_URL, title="网站导航")])

    hits = _research(client, fetcher).search([_query("数据出境安全评估")])

    assert hits == []
    assert fetcher.calls == [NEW_URL]


class _FakeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False


def test_exa_adapter_asks_for_urls_and_never_for_page_contents(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request: urllib.request.Request, timeout: int | None = None) -> _FakeResponse:
        captured["payload"] = json.loads((request.data or b"{}").decode("utf-8"))
        captured["url"] = request.full_url
        return _FakeResponse({"results": [{"url": KNOWN_URL, "title": "规定"}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = ExaSearchClient(api_key="test-key", base_url="https://api.exa.ai")

    results = client.search("数据出境", max_results=3)

    assert captured["url"] == "https://api.exa.ai/search"
    payload = captured["payload"]
    assert isinstance(payload, dict)
    assert payload["query"] == "数据出境"
    assert payload["numResults"] == 3
    assert payload["type"] == "auto"
    # The authoritative domains bound the search itself; the local filter is
    # only the second line of defence.
    assert payload["includeDomains"] == list(TRUSTED_SEARCH_DOMAINS)
    # Only URLs are requested, never page contents.
    assert "contents" not in payload
    assert results == [WebSearchResult(url=KNOWN_URL, title="规定")]


def test_exa_adapter_reports_a_transport_failure_instead_of_returning_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failing_urlopen(_request: urllib.request.Request, timeout: int | None = None) -> object:
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", failing_urlopen)
    client = ExaSearchClient(api_key="test-key")

    with pytest.raises(WebSearchUnavailable):
        client.search("数据出境", max_results=3)