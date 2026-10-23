"""Reproduce web-discovery boundaries without changing cases or the corpus."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import asdict
from datetime import date
from pathlib import Path
from unittest.mock import patch

import httpx

from law_agent.data.schemas import Chunk
from law_agent.review.agent import AgentStep
from law_agent.review.qa_agent import QAState, source_citations
from law_agent.review.schemas import RetrievalQuery, ReviewFacts
from law_agent.review.web_research import (
    ExaSearchClient,
    WebResearch,
    WebSearchResult,
    WebSearchUnavailable,
    build_web_search_client,
    canonical_url,
    is_official_url,
    is_public_web_url,
)


def query(text):
    return RetrievalQuery(query_id="q", query_type="legal_issue", text=text)


class Client:
    def __init__(self, results):
        self.results = results

    def search(self, text, *, max_results):
        result = self.results[text] if isinstance(self.results, dict) else self.results
        if isinstance(result, Exception):
            raise result
        return result[:max_results]


class RecordingClient:
    def __init__(self, client):
        self.client = client
        self.raw = []

    def search(self, query_text, *, max_results):
        results = self.client.search(query_text, max_results=max_results)
        self.raw.extend(asdict(item) for item in results)
        return results


def offline():
    checks = []

    def check(name, desired, actual, detail=None):
        checks.append({"name": name, "passed": desired == actual,
                       "expected": desired, "actual": actual, "detail": detail})

    for url, desired in [
        ("https://www.cac.gov.cn.evil.org/a", False),
        ("https://www.cac.gov.cn@evil.org/a", False),
        ("https://[invalid", False),
        ("https://segg.sh.gov.cn/a", True),
        ("https://eur-lex.europa.eu/a", True),
    ]:
        check("official_identity:" + url, desired, is_official_url(url))

    for url in ["http://127.0.0.1/a", "http://169.254.169.254/latest/meta-data/",
                "http://localhost/a", "https://example.org:invalid/a"]:
        check("public_url:" + url, False, is_public_web_url(url),
              "Only URL admission is tested; no URL is fetched.")

    def provider_response(body, status=200):
        response = httpx.Response(status, json=body,
                                  request=httpx.Request("POST", "https://api.exa.ai/search"))
        with patch("law_agent.review.web_research.httpx.post", return_value=response):
            try:
                result = ExaSearchClient(api_key="test").search("test", max_results=3)
                return {"outcome": "empty" if not result else "results",
                        "results": [asdict(item) for item in result]}
            except WebSearchUnavailable:
                return {"outcome": "error"}

    for body in [{}, {"error": "backend failure"}, {"results": {}}, []]:
        actual = provider_response(body)
        check("malformed_response:" + json.dumps(body), "error", actual["outcome"], actual)
    for status in [429, 500]:
        check(f"http_{status}", "error", provider_response({}, status)["outcome"])
    check("valid_empty_response", "empty", provider_response({"results": []})["outcome"])
    bad = provider_response({"results": [{"url": {"bad": "type"},
                                           "text": {"bad": "type"},
                                           "publishedDate": "not-a-date"}]})
    check("malformed_result_fields", "error", bad["outcome"], bad)

    partial = WebResearch(client=Client({
        "failed": WebSearchUnavailable("timeout"),
        "ok": [WebSearchResult(url="https://example.org/ok")],
    })).search([query("failed"), query("ok")])
    serialized = partial.model_dump(mode="json")
    check("partial_failure_is_visible", True,
          any(item.error for item in partial.query_results), serialized)

    check("different_ports_keep_distinct_identity", False,
          canonical_url("https://example.org:8443/a") == canonical_url("https://example.org/a"))
    check("tracking_query_deduplication", True,
          canonical_url("https://example.org/a?utm_source=x") == canonical_url("https://example.org/a"))

    chunk = Chunk(chunk_id="old:1", doc_id="old", source_id="old", title="old",
                  text="第一条\n第二条", chunk_index=0, char_count=7,
                  publish_date="2024-01-01", source_url="https://www.cac.gov.cn/a")
    finding = WebResearch(client=Client([
        WebSearchResult(url=chunk.source_url, text="第一条 第二条", published_date="2024-01-01"),
    ]), corpus_chunks=[chunk]).search([query("test")]).findings[0]
    check("whitespace_only_change_does_not_require_refresh", False, finding.refresh_needed)

    body = {"results": [{"url": "https://www.cac.gov.cn/long", "text": "前言" * 650 + "关键例外：测试条件不适用",
                         "highlights": ["关键例外：测试条件不适用"]}]}
    response = httpx.Response(200, json=body,
                             request=httpx.Request("POST", "https://api.exa.ai/search"))
    with patch("law_agent.review.web_research.httpx.post", return_value=response):
        finding = WebResearch(client=ExaSearchClient(api_key="test")).search([query("关键例外")]).findings[0]
    check("excerpt_respects_1200_limit", True, len(finding.excerpt) <= 1200)
    check("late_decisive_paragraph_is_available", True, "关键例外" in finding.excerpt,
          "The adapter consumes provider-selected highlights instead of the page prefix.")
    future = WebResearch(client=Client([
        WebSearchResult(url="https://www.cac.gov.cn/new", text="later rule",
                        published_date="2024-03-22"),
    ])).search([query("historical")], ReviewFacts(as_of_date=date(2023, 12, 31))).findings[0]
    citation = source_citations(QAState(
        goal="historical", facts=ReviewFacts(as_of_date=date(2023, 12, 31)),
        steps=[AgentStep(number=1, action="search_web", summary="test",
                         observation={"findings": [future.model_dump(mode="json")]})],
    ))[0]
    check("web_citation_retains_publication_date", True, "published_date" in citation,
          {"finding": future.model_dump(mode="json"), "citation": citation})
    check("future_web_citation_has_time_warning", True,
          citation.get("published_after_as_of") is True, citation)
    check("web_citation_is_not_formal_clause_evidence", False, citation["can_cite_clause"])
    return checks


LIVE_CASES = [
    ("consent_linkage", "向境外提供个人信息，依据个人信息保护法第十三条第二项至第七项处理时，与第三十九条单独同意要求如何衔接"),
    ("draft_vs_final", "个人信息出境标准合同办法 正式发布 征求意见稿 区别 生效日期"),
    ("historical_scope", "截至2023年12月31日 企业向境外传输员工个人信息 适用的数据出境规则"),
    ("attachment_scope", "上海自贸试验区 2025 数据出境负面清单 附件 适用主体 未涉及行业"),
    ("false_premise", "《中华人民共和国跨境员工数据自由流动法》2026年正式施行 第十八条 原文"),
    ("long_document", "中华人民共和国个人信息保护法 第七十三条 个人信息处理者 定义 原文"),
]


def live():
    client = build_web_search_client()
    records = []
    for name, text in LIVE_CASES:
        started = time.perf_counter()
        recording = RecordingClient(client)
        try:
            batch = WebResearch(client=recording).search([query(text)], ReviewFacts(as_of_date=date(2023, 12, 31)) if name == "historical_scope" else None, max_results=3)
            record = {"name": name, "query": text, "candidates": recording.raw, **batch.model_dump(mode="json"),
                      "batch_status": batch.status, "status": "error" if batch.status == "failed" else "success"}
        except WebSearchUnavailable as exc:
            record = {"name": name, "query": text, "status": "error", "error": str(exc)}
        record["elapsed_seconds"] = round(time.perf_counter() - started, 3)
        records.append(record)
        print(json.dumps({k: record[k] for k in ("name", "status", "elapsed_seconds")}), flush=True)
    return records


def fingerprints():
    paths = [Path("law_agent/review/web_research.py"), Path("law_agent/review/agent.py"),
             Path("law_agent/review/qa_agent.py"), Path("law_agent/review/schemas.py")]
    return {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Make exactly six Exa search requests.")
    parser.add_argument("--output", type=Path,
                        default=Path("data/review_runs/web_search_fix_20261023/results.json"))
    args = parser.parse_args()
    before = fingerprints()
    result = {"offline": offline(), "live": live() if args.live else [],
              "source_fingerprints_before": before, "source_fingerprints_after": fingerprints()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"offline_count": len(result["offline"]),
                      "offline_passed": sum(item["passed"] for item in result["offline"]),
                      "live_count": len(result["live"]), "output": str(args.output)}))


if __name__ == "__main__":
    main()
