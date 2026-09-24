"""Bounded public-web investigation for the compliance Agent.

The Agent may keep investigating outside the governed corpus, but only through
this narrow path: search a closed set of authoritative sites for candidate
URLs, read the real page body, chunk it, and hand the passages back as ordinary
``RetrievalHit`` records.

Two properties are deliberate:

* a search result only ever contributes its URL — the search provider's own
  snippet is never turned into evidence, because a snippet is not the page;
* the fetched page is written to a temporary directory, so a web page never
  enters the long-term corpus and no index is created for it.

Citability is not decided here. Every fetched page is normalized and chunked by
the existing ingestion pipeline, which already derives ``citation_role`` and
``can_cite_clause`` from the source id: a page matched back to a governed source
keeps that source's permissions, while a newly discovered page gets a
``web_<hash>`` id and therefore stays auxiliary.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit, urlunsplit

from law_agent.config import load_web_search_api_key
from law_agent.data.chunking.pipeline import chunk_document
from law_agent.data.fetchers.generic import FetchResult, fetch_source
from law_agent.data.normalize import normalize_source
from law_agent.data.schemas import Authority, Chunk, DocType, LawStatus, SourceRecord
from law_agent.review.retrieval.text import tokenize
from law_agent.review.schemas import RetrievalHit, RetrievalQuery, ReviewFacts

# Fixed limits for the bounded investigation. These are product decisions from
# the feature's design, not deployment knobs, so they are constants.
MAX_QUERIES_PER_SEARCH = 3
MAX_CHUNKS_PER_PAGE = 4
DEFAULT_MAX_PAGES = 3
REQUEST_TIMEOUT_SECONDS = 20
EXA_BASE_URL = "https://api.exa.ai"
PLAIN_TEXT_SUFFIXES = frozenset({".txt", ".text", ".md", ".markdown"})
# Document formats the ingestion pipeline parses properly. Their bodies are
# binary on purpose, so they must not be dismissed as unreadable binary noise.
DOCUMENT_FORMATS = frozenset({"pdf", "docx"})

# The discovery allowlist the production search is restricted to. The bound is
# applied by the provider before ranking, so the search pool is the authoritative
# set rather than a broad search trimmed afterwards: filtering after the fact
# could drop every official page just because a repost outranked it.
#
# This is a closed list on purpose: it holds the national aggregators, the
# judicial and procuratorial portals, and the regulators this product actually
# meets. Region and industry belong in the query, never in a growing domain map.
#
# No entry may be a bare ``www.`` host of an apex domain. Exa matches an entry
# against that host *and all of its subdomains* after stripping one leading
# ``www.``, so a plain ``www.gov.cn`` was measured to collapse to the ``gov.cn``
# apex and return county-level sites such as ``www.zhuoni.gov.cn``. The State
# Council portal is therefore left out of the search pool; its policy documents
# stay reachable through ``sousuo.www.gov.cn``, which carries no ``www.`` prefix
# and was measured to return that single host only.
TRUSTED_SEARCH_DOMAINS = (
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
)


class WebSearchUnavailable(RuntimeError):
    """Raised when the Agent asks for the web but no search provider can answer."""


@dataclass(frozen=True)
class WebSearchResult:
    """One candidate page discovered by the search provider."""

    url: str
    title: str = ""


class WebSearchClient(Protocol):
    """Minimal search-provider boundary: URL discovery only."""

    def search(self, query: str, *, max_results: int) -> list[WebSearchResult]: ...


class PageFetcher(Protocol):
    """Downloads one candidate page into a temporary directory."""

    def __call__(
        self, record: SourceRecord, output_dir: Path, *, timeout_seconds: int
    ) -> FetchResult: ...


class ExaSearchClient:
    """Thin Exa adapter. Only URLs are requested, never page contents."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = EXA_BASE_URL,
        timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
        include_domains: Sequence[str] = TRUSTED_SEARCH_DOMAINS,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.include_domains = tuple(include_domains)

    def search(self, query: str, *, max_results: int) -> list[WebSearchResult]:
        payload = {"query": query, "numResults": max_results, "type": "auto"}
        if self.include_domains:
            payload["includeDomains"] = list(self.include_domains)
        request = urllib.request.Request(
            f"{self.base_url}/search",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
            },
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
            WebSearchResult(url=str(item.get("url")), title=str(item.get("title") or ""))
            for item in results
            if isinstance(item, dict) and item.get("url")
        ]


def canonical_url(url: str) -> str:
    """Normalize a URL for identity comparison, ignoring presentation noise.

    Scheme is unified to https, the host is lowercased with a leading ``www.``
    dropped, and a trailing slash is removed. Query strings are kept: official
    Chinese sites such as ``flk.npc.gov.cn`` key their documents by query
    parameter, so dropping it would merge distinct documents.
    """

    parts = urlsplit(url.strip())
    host = parts.netloc.lower().removeprefix("www.")
    return urlunsplit(("https", host, parts.path.rstrip("/") or "/", parts.query, ""))


def host_of(url: str) -> str:
    """Return the comparable host of a URL, or an empty string when it has none."""

    host = urlsplit(url.strip()).netloc.lower().removeprefix("www.")
    return host


def _is_http_url(url: str) -> bool:
    return urlsplit(url.strip()).scheme in {"http", "https"}


@dataclass(frozen=True)
class KnownSource:
    """Metadata the corpus already holds for one governed source."""

    source_id: str
    title: str
    doc_type: DocType
    authority: Authority
    law_status: LawStatus
    publish_date: str | None
    effective_date: str | None
    issuing_body: str | None
    source_url: str


class CorpusSourceIndex:
    """Canonical URL -> source metadata map built from the governed corpus."""

    def __init__(self, chunks: Sequence[Chunk]) -> None:
        self._by_url: dict[str, KnownSource] = {}
        for chunk in chunks:
            url = chunk.source_url or ""
            if not _is_http_url(url):
                continue
            self._by_url.setdefault(
                canonical_url(url),
                KnownSource(
                    source_id=chunk.source_id,
                    title=chunk.title,
                    doc_type=chunk.doc_type,
                    authority=chunk.authority,
                    law_status=chunk.law_status,
                    publish_date=chunk.publish_date,
                    effective_date=chunk.effective_date,
                    issuing_body=chunk.issuing_body,
                    source_url=chunk.source_url,
                ),
            )

    def match(self, url: str) -> KnownSource | None:
        """Return the governed source this URL belongs to, if the corpus has it."""

        return self._by_url.get(canonical_url(url))


def _score(query_tokens: set[str], text: str) -> float:
    """Deterministic lexical relevance used to pick passages from one page.

    Three fetched pages do not justify a temporary embedding index, so the
    in-page selection stays a plain token-overlap ratio.
    """

    if not query_tokens:
        return 0.0
    tokens = set(tokenize(text))
    if not tokens:
        return 0.0
    return round(len(query_tokens & tokens) / len(query_tokens), 6)


def _looks_like_binary(raw: bytes) -> bool:
    if raw.startswith(b"%PDF"):
        return True
    return b"\x00" in raw[:4096]


def _file_format_for(url: str) -> str:
    """Decide which format a fetched page must be written and parsed as.

    The suffix is not cosmetic: naming a PDF ``.html`` hands its bytes to the
    HTML text extractor, which turns them into thousands of characters of
    binary noise. The formats named here are the ones the ingestion pipeline
    already knows how to name and parse.
    """

    suffix = Path(urlsplit(url).path).suffix.lower()
    if suffix in PLAIN_TEXT_SUFFIXES:
        return "txt"
    if suffix.lstrip(".") in DOCUMENT_FORMATS:
        return suffix.lstrip(".")
    if suffix == ".json":
        return "json"
    return "html"


class WebResearch:
    """Search, read and chunk official pages within one Agent run."""

    def __init__(
        self,
        *,
        client: WebSearchClient,
        corpus_chunks: Sequence[Chunk] = (),
        max_pages: int = DEFAULT_MAX_PAGES,
        fetch_timeout_seconds: int = REQUEST_TIMEOUT_SECONDS,
        fetch: PageFetcher | None = None,
    ) -> None:
        self._client = client
        self._max_pages = max_pages
        self._fetch_timeout_seconds = fetch_timeout_seconds
        self._fetch: PageFetcher = fetch or _default_fetch
        self._index = CorpusSourceIndex(corpus_chunks)

    def search(
        self,
        queries: Sequence[RetrievalQuery],
        facts: ReviewFacts | None = None,
    ) -> list[RetrievalHit]:
        """Resolve queries into evidence read from official page bodies.

        A failing page is skipped with no effect on the rest of the search. A
        failing provider is only reported when nothing at all could be read, so
        a partial success still reaches the Agent.
        """

        del facts  # facts only shape the local corpus boosts, not page selection
        hits: list[RetrievalHit] = []
        seen_pages: set[str] = set()
        pages_read = 0
        provider_error: WebSearchUnavailable | None = None

        for query in list(queries)[:MAX_QUERIES_PER_SEARCH]:
            if pages_read >= self._max_pages:
                break
            try:
                candidates = self._client.search(query.text, max_results=self._max_pages)
            except WebSearchUnavailable as exc:
                provider_error = exc
                continue
            for candidate in self._candidates(candidates):
                if pages_read >= self._max_pages:
                    break
                key = canonical_url(candidate.url)
                if key in seen_pages:
                    continue
                seen_pages.add(key)
                pages_read += 1
                hits.extend(self._read_page(candidate, query))

        if not hits and provider_error is not None:
            raise provider_error
        return [hit.model_copy(update={"rank": rank}) for rank, hit in enumerate(hits)]

    def _candidates(self, results: Sequence[WebSearchResult]) -> list[WebSearchResult]:
        """Keep the provider's ranking; only drop entries that are not URLs.

        Which sites may be searched is settled upstream by ``includeDomains``, so
        nothing here decides whether a host is trustworthy.
        """

        return [item for item in results if _is_http_url(item.url)]

    def _read_page(
        self, candidate: WebSearchResult, query: RetrievalQuery
    ) -> list[RetrievalHit]:
        record = self._source_record(candidate)
        try:
            with tempfile.TemporaryDirectory() as directory:
                result = self._fetch(
                    record, Path(directory), timeout_seconds=self._fetch_timeout_seconds
                )
                if not result.ok or not result.path.exists():
                    return []
                if record.file_format not in DOCUMENT_FORMATS and _looks_like_binary(
                    result.path.read_bytes()
                ):
                    return []
                document = normalize_source(record, result.path, parser="auto")
                chunks = chunk_document(document)
        except (RuntimeError, ValueError, OSError):
            return []

        query_tokens = set(tokenize(query.text))
        scored = [
            (_score(query_tokens, chunk.text), chunk)
            for chunk in chunks
            if chunk.text.strip()
        ]
        # Real pages carry navigation and footer boilerplate as their own
        # blocks. Without query overlap those chunks are only reachable through
        # ``chunk_index``, so dropping them keeps the page budget on passages
        # that can actually support a conclusion.
        if query_tokens:
            scored = [item for item in scored if item[0] > 0]
        scored.sort(key=lambda item: (-item[0], item[1].chunk_index))
        return [
            _web_hit(chunk, record, score, rank, query)
            for rank, (score, chunk) in enumerate(scored[:MAX_CHUNKS_PER_PAGE])
        ]

    def _source_record(self, candidate: WebSearchResult) -> SourceRecord:
        """Reuse the governed identity for a known URL, otherwise mint a web id."""

        known = self._index.match(candidate.url)
        if known is not None:
            return SourceRecord(
                source_id=known.source_id,
                title=known.title,
                source_url=candidate.url,
                source_site=host_of(candidate.url),
                doc_type=known.doc_type,
                authority=known.authority,
                law_status=known.law_status,
                publish_date=known.publish_date,
                effective_date=known.effective_date,
                issuing_body=known.issuing_body,
                file_format=_file_format_for(candidate.url),
            )
        digest = hashlib.sha256(canonical_url(candidate.url).encode("utf-8")).hexdigest()[:12]
        return SourceRecord(
            source_id=f"web_{digest}",
            title=candidate.title or candidate.url,
            source_url=candidate.url,
            source_site=host_of(candidate.url),
            doc_type="policy",
            file_format=_file_format_for(candidate.url),
        )


def _web_hit(
    chunk: Chunk,
    record: SourceRecord,
    score: float,
    rank: int,
    query: RetrievalQuery,
) -> RetrievalHit:
    """Express a fetched page passage as an ordinary retrieval hit.

    The chunk id is namespaced because a re-read official page numbers its own
    chunks from zero; without the prefix a live page could silently overwrite
    the governed corpus chunk that happens to share the same id.
    """

    return RetrievalHit(
        chunk_id=f"web:{record.source_id}:{chunk.chunk_index:04d}",
        doc_id=record.source_id,
        source_id=record.source_id,
        title=record.title or chunk.title,
        text=chunk.text,
        score=score,
        rank=rank,
        retriever="web",
        citation_role=chunk.citation_role,
        can_cite_clause=chunk.can_cite_clause,
        source_url=record.source_url,
        matched_query_type=query.query_type,
        article_no=chunk.article_no,
        citation_label=chunk.citation_label,
        heading_path=chunk.heading_path,
        doc_type=chunk.doc_type,
        authority=chunk.authority,
        law_status=chunk.law_status,
        publish_date=chunk.publish_date,
        effective_date=chunk.effective_date,
        issuing_body=chunk.issuing_body,
    )


def _default_fetch(record: SourceRecord, output_dir: Path, *, timeout_seconds: int) -> FetchResult:
    return fetch_source(record, output_dir, timeout_seconds=timeout_seconds)


def build_web_search_client() -> WebSearchClient:
    """Build the production search client, or fail when it is not configured."""

    api_key = load_web_search_api_key()
    if not api_key:
        raise WebSearchUnavailable(
            "Web 搜索未配置：请设置 EXA_API_KEY 后重试；本次只能使用受控法律库。"
        )
    return ExaSearchClient(api_key=api_key)