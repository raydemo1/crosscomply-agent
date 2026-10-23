"""Bounded public web discovery, separate from legal evidence qualification."""

from __future__ import annotations

import re
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime
from ipaddress import ip_address
from typing import Literal, Protocol
from urllib.parse import unquote_plus, urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from law_agent.config import load_web_search_api_key
from law_agent.data.schemas import Chunk, StrictModel
from law_agent.review.dates import business_today
from law_agent.review.schemas import RetrievalQuery, ReviewFacts

MAX_QUERIES_PER_SEARCH = 4
DEFAULT_WEB_RESULTS = 3
MAX_WEB_RESULTS = 8
MAX_EXCERPT_CHARACTERS = 1200
MAX_TITLE_CHARACTERS = 500
MAX_URL_CHARACTERS = 4096
REQUEST_TIMEOUT_SECONDS = 20
EXA_BASE_URL = "https://api.exa.ai"
OFFICIAL_DOMAIN_SUFFIXES = ("gov.cn", "gov", "gov.uk", "gov.hk", "gov.mo", "europa.eu")
OFFICIAL_DOMAINS = ("tc260.org.cn",)


def web_search_scope() -> dict:
    return {"mode": "open_web", "source_preference": "官方站点软优先，不排除其他公开来源",
            "result_role": "按查询抽取的有限原文段落，可能不连续，不代表完整正文或正式法条资格",
            "date_policy": "发布时间不是施行日期；晚于判断时点的网页仅供追溯或比较，不直接证明当时规则",
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
    excerpt_type: Literal["query_highlights", "none"] = "none"
    excerpt_truncated: bool = False
    excerpt_query_id: str | None = None
    published_after_as_of: bool | None = None

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
    excerpt_truncated: bool = False


class WebQueryResult(StrictModel):
    query_id: str
    query_index: int
    status: Literal["success", "empty", "error"]
    candidate_count: int = 0
    rejected_count: int = 0
    result_urls: list[str] = Field(default_factory=list)
    returned_urls: list[str] = Field(default_factory=list)
    error: str | None = None


class WebSearchBatch(StrictModel):
    findings: list[WebFinding] = Field(default_factory=list)
    query_results: list[WebQueryResult] = Field(default_factory=list)
    status: Literal["complete", "partial", "failed"] = "complete"
    as_of_date: date = Field(default_factory=business_today)


class _ExaResult(BaseModel):
    model_config = ConfigDict(strict=True)

    url: str = Field(min_length=1, max_length=MAX_URL_CHARACTERS)
    title: str | None = None
    highlights: list[str] = Field(default_factory=list)
    publishedDate: str | None = None

    @field_validator("publishedDate")
    @classmethod
    def publication_date(cls, value):
        if value is None:
            return None
        parsed = datetime.fromisoformat(value)
        return parsed.date().isoformat()


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
            "contents": {"highlights": {"query": query, "maxCharacters": MAX_EXCERPT_CHARACTERS}},
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
        if not isinstance(results, list) or "error" in body:
            raise WebSearchUnavailable("Exa 搜索响应格式错误：未取得合法 results 数组")
        try:
            validated = [_ExaResult.model_validate(item) for item in results]
        except ValidationError as exc:
            raise WebSearchUnavailable("Exa 搜索响应格式错误：结果字段类型或日期无效") from exc
        parsed = []
        for item in validated[:max_results]:
            excerpt = "\n\n[…]\n\n".join(part.strip() for part in item.highlights if part.strip())
            parsed.append(WebSearchResult(
                url=item.url, title=(item.title or "")[:MAX_TITLE_CHARACTERS],
                text=excerpt[:MAX_EXCERPT_CHARACTERS], published_date=item.publishedDate,
                excerpt_truncated=len(excerpt) > MAX_EXCERPT_CHARACTERS,
            ))
        return parsed


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower().rstrip(".").removeprefix("www.")
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and (parts.scheme, port) not in {("http", 80), ("https", 443)}:
        host = f"{host}:{port}"
    query = "&".join(pair for pair in parts.query.split("&") if pair and not (
        unquote_plus(pair.partition("=")[0]).lower().startswith("utm_")
        or unquote_plus(pair.partition("=")[0]).lower() in {"fbclid", "gclid", "msclkid"}
    ))
    return urlunsplit(("https", host, parts.path.rstrip("/") or "/", query, ""))


def host_of(url: str) -> str:
    return (urlsplit(url.strip()).hostname or "").lower().rstrip(".").removeprefix("www.")


def is_public_web_url(url: str) -> bool:
    try:
        if len(url) > MAX_URL_CHARACTERS or any(ord(char) <= 32 or ord(char) == 127 for char in url):
            return False
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower().rstrip(".")
        port = parts.port
        if port == 0 or not host or parts.scheme not in {"http", "https"} or parts.username or parts.password:
            return False
        try:
            address = ip_address(host)
            return address.is_global and not address.is_multicast
        except ValueError:
            pass
        labels = host.encode("idna").decode("ascii").split(".")
    except (ValueError, UnicodeError):
        return False
    return (len(host) <= 253 and len(labels) >= 2 and not labels[-1].isdigit()
            and labels[-1] not in {"localhost", "local", "internal", "test", "invalid"}
            and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels))


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
    ) -> WebSearchBatch:
        if type(max_results) is not int or not 1 <= max_results <= MAX_WEB_RESULTS:
            raise ValueError(f"max_results必须是1至{MAX_WEB_RESULTS}的整数，表示本批查询的结果总数上限")
        batch = list(queries)
        if not 1 <= len(batch) <= MAX_QUERIES_PER_SEARCH or any(
            not query.text.strip() or len(query.text) > 1000 for query in batch
        ):
            raise ValueError("Web 搜索需要1至4个非空查询，每个不超过1000字符")

        candidate_limit = min(MAX_WEB_RESULTS, max_results + 2)
        as_of = (facts.as_of_date if facts else None) or business_today()

        def search_one(query):
            try:
                return self._client.search(query.text, max_results=candidate_limit)
            except WebSearchUnavailable as exc:
                return exc

        with ThreadPoolExecutor(max_workers=len(batch)) as executor:
            batches = list(executor.map(search_one, batch))
        findings: list[WebFinding] = []
        seen: set[str] = set()
        query_results = []
        ranked = []
        for query_index, (query, results) in enumerate(zip(batch, batches, strict=True)):
            if isinstance(results, WebSearchUnavailable):
                query_results.append(WebQueryResult(
                    query_id=query.query_id, query_index=query_index, status="error", error=str(results)[:2000],
                ))
                continue
            candidates = [(index, item) for index, item in enumerate(results[:candidate_limit])
                          if is_public_web_url(item.url)]
            query_results.append(WebQueryResult(
                query_id=query.query_id, query_index=query_index, status="success" if candidates else "empty",
                candidate_count=len(results), rejected_count=len(results[:candidate_limit]) - len(candidates),
                result_urls=[item.url for _, item in candidates],
            ))
            ranked.append((query_index, [item for _, item in sorted(
                candidates, key=lambda entry: entry[0] - (1.25 if is_official_url(entry[1].url) else 0),
            )]))
        for index in range(candidate_limit):
            for query_index, results in ranked:
                item = next((item for item in results[index:] if canonical_url(item.url) not in seen), None)
                if item is None:
                    continue
                if len(findings) >= max_results:
                    break
                key = canonical_url(item.url)
                seen.add(key)
                query_results[query_index].returned_urls.append(item.url)
                known_id = self._known.get(key)
                excerpt = item.text.strip()[:MAX_EXCERPT_CHARACTERS]
                newer_date = bool(item.published_date and item.published_date > self._known_dates.get(key, ""))
                known_text = " ".join(" ".join(self._known_text.get(key, [])).split())
                passages = [" ".join(part.split()) for part in re.split(r"\[…\]|\.{3,}|…+", excerpt)]
                changed_excerpt = any(part and part not in known_text for part in passages)
                findings.append(WebFinding(
                    title=(item.title or item.url)[:MAX_TITLE_CHARACTERS], url=item.url,
                    excerpt=excerpt, known_source_id=known_id,
                    published_date=item.published_date,
                    refresh_needed=bool(known_id and (newer_date or changed_excerpt)),
                    excerpt_type="query_highlights" if excerpt else "none",
                    excerpt_truncated=item.excerpt_truncated or len(item.text.strip()) > MAX_EXCERPT_CHARACTERS,
                    excerpt_query_id=batch[query_index].query_id,
                    published_after_as_of=(date.fromisoformat(item.published_date) > as_of
                                           if item.published_date and as_of else None),
                ))
        failures = sum(item.status == "error" for item in query_results)
        return WebSearchBatch(
            findings=findings, query_results=query_results,
            status="failed" if failures == len(batch) else "partial" if failures else "complete",
            as_of_date=as_of,
        )


def build_web_search_client() -> WebSearchClient:
    api_key = load_web_search_api_key()
    if not api_key:
        raise WebSearchUnavailable("Web 搜索未配置：请设置 EXA_API_KEY 后重试；本次只能使用受控法律库。")
    return ExaSearchClient(api_key=api_key)
