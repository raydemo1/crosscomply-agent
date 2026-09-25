"""Bounded official web findings, never legal evidence."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol
from urllib.parse import urlsplit, urlunsplit

from law_agent.config import load_web_search_api_key
from law_agent.data.schemas import Chunk, StrictModel
from law_agent.review.schemas import RetrievalQuery, ReviewFacts

MAX_QUERIES_PER_SEARCH = 3
DEFAULT_MAX_PAGES = 3
MAX_WEB_TEXT_CHARACTERS = 6000
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
    published_date: str | None = None
    excerpt: str = ""
    known_source_id: str | None = None
    refresh_needed: bool = False
    status: Literal["read", "discovered"] = "discovered"


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
        include_domains: Sequence[str] = TRUSTED_SEARCH_DOMAINS,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.include_domains = tuple(include_domains)

    def search(self, query: str, *, max_results: int) -> list[WebSearchResult]:
        payload = {
            "query": query, "numResults": max_results, "type": "auto",
            "contents": {"text": {"maxCharacters": MAX_WEB_TEXT_CHARACTERS}},
        }
        if self.include_domains:
            payload["includeDomains"] = list(self.include_domains)
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
                text=str(item.get("text") or "")[:MAX_WEB_TEXT_CHARACTERS],
                published_date=item.get("publishedDate"),
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
    parts = urlsplit(url.strip())
    if parts.scheme not in {"http", "https"} or parts.username or parts.password:
        return False
    host = (parts.hostname or "").lower()
    return any(host == domain or host == domain.removeprefix("www.") for domain in domains)


class WebResearch:
    def __init__(
        self, *, client: WebSearchClient, corpus_chunks: Sequence[Chunk] = (),
        max_pages: int = DEFAULT_MAX_PAGES,
    ) -> None:
        self._client = client
        self._max_pages = max_pages
        self._known: dict[str, tuple[str, str | None]] = {}
        for chunk in corpus_chunks:
            if not chunk.source_url:
                continue
            key = canonical_url(chunk.source_url)
            previous = self._known.get(key)
            if previous is None or (chunk.publish_date or "") > (previous[1] or ""):
                self._known[key] = (chunk.source_id, chunk.publish_date)

    def search(
        self, queries: Sequence[RetrievalQuery], facts: ReviewFacts | None = None,
    ) -> list[WebFinding]:
        del facts
        findings: list[WebFinding] = []
        seen: set[str] = set()
        provider_error: WebSearchUnavailable | None = None
        for query in list(queries)[:MAX_QUERIES_PER_SEARCH]:
            if len(findings) >= self._max_pages:
                break
            try:
                results = self._client.search(query.text, max_results=self._max_pages)
            except WebSearchUnavailable as exc:
                provider_error = exc
                continue
            for item in results:
                if len(findings) >= self._max_pages:
                    break
                key = canonical_url(item.url)
                if key in seen or not is_trusted_official_url(item.url):
                    continue
                seen.add(key)
                excerpt = item.text.strip()[:MAX_WEB_TEXT_CHARACTERS]
                findings.append(WebFinding(
                    title=item.title or item.url, url=item.url,
                    published_date=item.published_date, excerpt=excerpt,
                    known_source_id=self._known[key][0] if key in self._known else None,
                    refresh_needed=bool(
                        key in self._known and item.published_date
                        and (
                            not self._known[key][1]
                            or item.published_date[:10] > self._known[key][1][:10]
                        )
                    ),
                    status="read" if excerpt else "discovered",
                ))
        if not findings and provider_error is not None:
            raise provider_error
        return findings


def build_web_search_client() -> WebSearchClient:
    api_key = load_web_search_api_key()
    if not api_key:
        raise WebSearchUnavailable("Web 搜索未配置：请设置 EXA_API_KEY 后重试；本次只能使用受控法律库。")
    return ExaSearchClient(api_key=api_key)
