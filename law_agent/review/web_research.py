"""Bounded public web discovery, separate from legal evidence qualification."""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import model_validator

from law_agent.config import load_web_search_api_key
from law_agent.data.schemas import Chunk, StrictModel
from law_agent.review.schemas import RetrievalQuery, ReviewFacts

MAX_QUERIES_PER_SEARCH = 4
DEFAULT_WEB_RESULTS = 3
MAX_WEB_RESULTS = 8
MAX_EXCERPT_CHARACTERS = 1200
REQUEST_TIMEOUT_SECONDS = 20
EXA_BASE_URL = "https://api.exa.ai"
OFFICIAL_DOMAIN_SUFFIXES = ("gov.cn", "gov", "gov.uk", "gov.hk", "gov.mo", "europa.eu")
OFFICIAL_DOMAINS = ("tc260.org.cn",)


def web_search_scope() -> dict:
    return {"mode": "open_web", "source_preference": "官方站点软优先，不排除其他公开来源",
            "result_role": "有长度上限的搜索摘录，不代表完整正文或正式法条资格",
            "max_queries": MAX_QUERIES_PER_SEARCH,
            "default_max_results": DEFAULT_WEB_RESULTS, "max_results_limit": MAX_WEB_RESULTS,
            "max_excerpt_characters": MAX_EXCERPT_CHARACTERS}


class WebSearchUnavailable(RuntimeError):
    pass


class WebFinding(StrictModel):
    title: str
    url: str
    excerpt: str = ""
    known_source_id: str | None = None
    published_date: str | None = None
    refresh_needed: bool = False
    is_official: bool = False

    @model_validator(mode="after")
    def classify_site(self):
        self.is_official = is_official_url(self.url)
        return self


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
        }
        try:
            response = httpx.post(
                f"{self.base_url}/search", json=payload,
                headers={"Accept": "application/json", "x-api-key": self.api_key},
                timeout=self.timeout_seconds, follow_redirects=False,
            )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, OSError, ValueError) as exc:
            raise WebSearchUnavailable(f"Exa 搜索请求失败：{exc}") from exc
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


def is_public_web_url(url: str) -> bool:
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return bool(host and parts.scheme in {"http", "https"} and not parts.username and not parts.password)


def is_official_url(url: str) -> bool:
    if not is_public_web_url(url):
        return False
    host = host_of(url)
    return host in OFFICIAL_DOMAINS or any(
        host == suffix or host.endswith("." + suffix) for suffix in OFFICIAL_DOMAIN_SUFFIXES
    )


def eligible_material_web_urls(findings: Sequence[WebFinding]) -> list[str]:
    return list({
        canonical_url(item.url): item.url for item in findings
        if is_official_url(item.url) and (item.known_source_id is None or item.refresh_needed)
    }.values())


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
        *, max_results: int = DEFAULT_WEB_RESULTS,
    ) -> list[WebFinding]:
        del facts
        if type(max_results) is not int or not 1 <= max_results <= MAX_WEB_RESULTS:
            raise ValueError(f"max_results必须是1至{MAX_WEB_RESULTS}的整数，表示本批查询的结果总数上限")
        batch = list(queries)
        if not 1 <= len(batch) <= MAX_QUERIES_PER_SEARCH or any(
            not query.text.strip() or len(query.text) > 1000 for query in batch
        ):
            raise ValueError("Web 搜索需要1至4个非空查询，每个不超过1000字符")

        candidate_limit = min(MAX_WEB_RESULTS, max_results + 2)

        def search_one(query):
            try:
                return self._client.search(query.text, max_results=candidate_limit)
            except WebSearchUnavailable as exc:
                return exc

        with ThreadPoolExecutor(max_workers=len(batch)) as executor:
            batches = list(executor.map(search_one, batch))
        findings: list[WebFinding] = []
        seen: set[str] = set()
        provider_error: WebSearchUnavailable | None = None
        ranked = []
        for results in batches:
            if isinstance(results, WebSearchUnavailable):
                provider_error = results
                continue
            candidates = [(index, item) for index, item in enumerate(results[:candidate_limit])
                          if is_public_web_url(item.url)]
            ranked.append([item for _, item in sorted(
                candidates, key=lambda entry: entry[0] - (1.25 if is_official_url(entry[1].url) else 0),
            )])
        for index in range(candidate_limit):
            for results in ranked:
                item = next((item for item in results[index:] if canonical_url(item.url) not in seen), None)
                if item is None:
                    continue
                if len(findings) >= max_results:
                    break
                key = canonical_url(item.url)
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
