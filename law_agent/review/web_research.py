"""Bounded official web findings, never legal evidence."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from law_agent.config import load_web_search_api_key
from law_agent.data.schemas import Chunk, StrictModel
from law_agent.review.schemas import RetrievalQuery, ReviewFacts

MAX_QUERIES_PER_SEARCH = 3
MAX_FINDINGS = 3
MAX_EXCERPT_CHARACTERS = 1200
REQUEST_TIMEOUT_SECONDS = 20
EXA_BASE_URL = "https://api.exa.ai"
TRUSTED_SEARCH_DOMAINS = (
    "flk.npc.gov.cn", "sousuo.www.gov.cn", "xzfg.moj.gov.cn",
    "www.moj.gov.cn", "www.court.gov.cn", "rmfyalk.court.gov.cn",
    "www.spp.gov.cn", "www.cac.gov.cn", "www.miit.gov.cn",
    "openstd.samr.gov.cn", "www.mnr.gov.cn",
)


class WebSearchUnavailable(RuntimeError):
    pass


class WebFinding(StrictModel):
    title: str
    url: str
    excerpt: str = ""
    known_source_id: str | None = None
    published_date: str | None = None
    refresh_needed: bool = False


@dataclass(frozen=True)
class WebSearchResult:
    url: str
    title: str = ""
    text: str = ""
    published_date: str | None = None


class WebSearchClient(Protocol):
    def search(self, query: str, *, max_results: int) -> list[WebSearchResult]: ...


class ExaSearchClient:
    def __init__(
        self, *, api_key: str, base_url: str = EXA_BASE_URL,
        timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def search(self, query: str, *, max_results: int) -> list[WebSearchResult]:
        payload = {
            "query": query, "numResults": max_results, "type": "auto",
            "contents": {"text": {"maxCharacters": MAX_EXCERPT_CHARACTERS}},
            "includeDomains": list(TRUSTED_SEARCH_DOMAINS),
        }
        request = urllib.request.Request(
            f"{self.base_url}/search",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Accept": "application/json", "Content-Type": "application/json", "x-api-key": self.api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_seconds) as response:
                body = json.loads(response.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise WebSearchUnavailable(f"Web 搜索请求失败：{exc}") from exc
        results = body.get("results") if isinstance(body, dict) else None
        if not isinstance(results, list):
            return []
        return [
            WebSearchResult(
                url=str(item["url"]), title=str(item.get("title") or ""),
                text=str(item.get("text") or "")[:MAX_EXCERPT_CHARACTERS],
                published_date=str(item["publishedDate"])[:10] if item.get("publishedDate") else None,
            )
            for item in results if isinstance(item, dict) and item.get("url")
        ]


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().removeprefix("www.")
    return urlunsplit(("https", host, parts.path.rstrip("/") or "/", parts.query, ""))


def host_of(url: str) -> str:
    return (urlsplit(url.strip()).hostname or "").lower().removeprefix("www.")


def is_trusted_official_url(url: str, domains: Sequence[str] = TRUSTED_SEARCH_DOMAINS) -> bool:
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if parts.scheme not in {"http", "https"} or parts.username or parts.password:
        return False
    return any(host == domain or host == domain.removeprefix("www.") for domain in domains)


class WebResearch:
    def __init__(
        self, *, client: WebSearchClient, corpus_chunks: Sequence[Chunk] = (),
    ) -> None:
        self._client = client
        self._known: dict[str, str] = {}
        self._known_dates: dict[str, str] = {}
        self._known_text: dict[str, list[str]] = {}
        for chunk in corpus_chunks:
            if not chunk.source_url:
                continue
            key = canonical_url(chunk.source_url)
            self._known[key] = chunk.source_id
            if chunk.publish_date:
                self._known_dates[key] = max(self._known_dates.get(key, ""), chunk.publish_date)
            self._known_text.setdefault(key, []).append(chunk.text)

    def search(
        self, queries: Sequence[RetrievalQuery], facts: ReviewFacts | None = None,
    ) -> list[WebFinding]:
        del facts
        findings: list[WebFinding] = []
        seen: set[str] = set()
        provider_error: WebSearchUnavailable | None = None
        for query in list(queries)[:MAX_QUERIES_PER_SEARCH]:
            if len(findings) >= MAX_FINDINGS:
                break
            try:
                results = self._client.search(query.text, max_results=MAX_FINDINGS)
            except WebSearchUnavailable as exc:
                provider_error = exc
                continue
            for item in results:
                if len(findings) >= MAX_FINDINGS:
                    break
                if not is_trusted_official_url(item.url):
                    continue
                key = canonical_url(item.url)
                if key in seen:
                    continue
                seen.add(key)
                known_id = self._known.get(key)
                excerpt = item.text.strip()[:MAX_EXCERPT_CHARACTERS]
                newer_date = bool(item.published_date and item.published_date > self._known_dates.get(key, ""))
                changed_excerpt = bool(excerpt and excerpt not in "\n".join(self._known_text.get(key, [])))
                findings.append(WebFinding(
                    title=item.title or item.url, url=item.url,
                    excerpt=excerpt, known_source_id=known_id,
                    published_date=item.published_date,
                    refresh_needed=bool(known_id and (newer_date or changed_excerpt)),
                ))
        if not findings and provider_error is not None:
            raise provider_error
        return findings


def build_web_search_client() -> WebSearchClient:
    api_key = load_web_search_api_key()
    if not api_key:
        raise WebSearchUnavailable("Web 搜索未配置：请设置 EXA_API_KEY 后重试；本次只能使用受控法律库。")
    return ExaSearchClient(api_key=api_key)
