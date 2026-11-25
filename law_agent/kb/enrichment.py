"""Persistent, fail-closed official-source enrichment."""

from __future__ import annotations

import hashlib
import re
import ssl
import urllib.error
import urllib.request
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from law_agent.data.html_content import attachment_links
from law_agent.data.normalize import _decode_html
from law_agent.data.schemas import SourceRecord
from law_agent.data.source_rules import metadata_problems, require_consistent_metadata
from law_agent.kb.admin import KnowledgeBaseAdminService
from law_agent.kb.ingestion import (
    PreparedSource,
    parse_document,
    prepare_bound_document,
    prepare_document_for_ingest,
    provisional_source,
)
from law_agent.kb.service import normalized_content_hash
from law_agent.kb.source_inference import (
    SourceInference,
    clean_title,
    previous_editions,
    retire_editions,
    supersession_problem,
    version_date,
)
from law_agent.kb.source_llm import read_source_metadata, trim_web_body
from law_agent.review.web_research import (
    WebFinding,
    canonical_url,
    host_of,
    is_official_url,
)

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
# Instruments a worker may publish without a reviewer once every field is settled: enacted norms,
# whose clause-level citability a wrong guess would grant. Everything else waits for one click.
AUTO_PUBLISH_DOC_TYPES = frozenset({"law", "regulation", "judicial_interpretation"})
DRAFT_CHUNK_SAMPLES = 6
# What a notice-only page's attachment can be parsed as.
PARSEABLE_ATTACHMENTS = frozenset({".pdf", ".docx", ".txt"})
_ATTACHMENT_PREFIX_RE = re.compile(r"^(?:附件\s*\d*\s*[:：.、．]?\s*|\d+\s*[.、．]\s*)")


def attachment_title(name: str) -> str:
    """"附件1：证券期货业网络和信息安全管理办法.pdf" → "证券期货业网络和信息安全管理办法"."""

    name = re.sub(r"\.(?:pdf|docx?|ofd|wps|txt|xlsx?|zip|rar)$", "", name.strip(), flags=re.IGNORECASE)
    return clean_title(_ATTACHMENT_PREFIX_RE.sub("", name))


class PostgresEnrichmentStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def enqueue(self, finding: WebFinding, *, case_id: str, review_task_id: str) -> str | None:
        if not is_official_url(finding.url) or (finding.known_source_id and not finding.refresh_needed):
            return None
        key = canonical_url(finding.url)
        candidate_hash = hashlib.sha256(
            (finding.title + "\x1f" + (finding.published_date or "") + "\x1f"
             + (finding.known_source_id or "") + "\x1f" + finding.excerpt).encode("utf-8")
        ).hexdigest()
        with psycopg.connect(self.dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """INSERT INTO knowledge_enrichment_jobs
                   (id, canonical_url, candidate_hash, url, title, excerpt, published_date)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (canonical_url, candidate_hash) DO UPDATE SET updated_at = now()
                   RETURNING id""",
                (f"enrich_{uuid4().hex}", key, candidate_hash, finding.url, finding.title, finding.excerpt,
                 finding.published_date),
            )
            job_id = cur.fetchone()["id"]
            cur.execute(
                """INSERT INTO knowledge_enrichment_cases (job_id, case_id, review_task_id)
                   VALUES (%s, %s, %s)
                   ON CONFLICT (job_id, case_id) DO UPDATE
                   SET review_task_id = EXCLUDED.review_task_id""",
                (job_id, case_id, review_task_id),
            )
            return job_id

    def expand(self, job_id: str, *, attachments: list[tuple[str, str]], context_text: str,
               published_date: str | None) -> list[str]:
        """Queue one child job per attached file of a notice and retire the notice itself.

        The cases waiting on the notice wait on its attachments instead: their links are copied to
        each child, and the notice's own links stop asking for a recheck."""

        children: list[str] = []
        with psycopg.connect(self.dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
            for title, url in attachments:
                cur.execute(
                    """INSERT INTO knowledge_enrichment_jobs
                       (id, canonical_url, candidate_hash, url, title, excerpt, published_date, parent_job_id,
                        context_text)
                       VALUES (%s, %s, %s, %s, %s, '', %s, %s, %s)
                       ON CONFLICT (canonical_url, candidate_hash) DO UPDATE SET updated_at = now()
                       RETURNING id""",
                    (f"enrich_{uuid4().hex}", canonical_url(url),
                     hashlib.sha256(f"{title}\x1f{job_id}".encode()).hexdigest(), url, title, published_date,
                     job_id, context_text),
                )
                child = cur.fetchone()["id"]
                children.append(child)
                cur.execute(
                    """INSERT INTO knowledge_enrichment_cases
                       (job_id, case_id, review_task_id, material, recheck_status, notification_status)
                       SELECT %s, case_id, review_task_id, material, recheck_status, notification_status
                       FROM knowledge_enrichment_cases WHERE job_id = %s
                       ON CONFLICT (job_id, case_id) DO NOTHING""",
                    (child, job_id),
                )
            cur.execute(
                """UPDATE knowledge_enrichment_cases SET material = false, recheck_status = 'not_required',
                       notification_status = 'not_required' WHERE job_id = %s""", (job_id,))
            cur.execute(
                """UPDATE knowledge_enrichment_jobs SET status = 'expanded', worker_id = NULL,
                       lease_expires_at = NULL, error = NULL, updated_at = now() WHERE id = %s""", (job_id,))
        return children

    def mark_impact(self, *, case_id: str, review_task_id: str, urls: list[str]) -> None:
        if not urls:
            return
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE knowledge_enrichment_cases ec SET material = true,
                       recheck_status = CASE WHEN j.status IN ('published', 'unchanged') THEN 'pending' ELSE 'waiting_source' END,
                       notification_status = CASE WHEN j.status IN ('published', 'unchanged') THEN 'pending' ELSE 'not_required' END
                   FROM knowledge_enrichment_jobs j
                   WHERE ec.job_id = j.id AND ec.case_id = %s AND ec.review_task_id = %s
                     AND ec.recheck_status IN ('not_required', 'waiting_source')
                     AND j.status <> 'expanded'
                     AND (j.canonical_url = ANY(%s) OR j.parent_job_id IN (
                          SELECT id FROM knowledge_enrichment_jobs WHERE canonical_url = ANY(%s)))""",
                (case_id, review_task_id, keys := [canonical_url(url) for url in urls], keys),
            )
            cur.execute(
                """INSERT INTO case_events (id, case_id, event_type, payload_json)
                   SELECT 'evt_' || md5(ec.job_id || ':' || ec.case_id),
                          ec.case_id, 'knowledge_recheck_pending',
                          jsonb_build_object('job_id', ec.job_id, 'source_id', j.source_id)
                   FROM knowledge_enrichment_cases ec
                   JOIN knowledge_enrichment_jobs j ON j.id=ec.job_id
                   WHERE ec.case_id=%s AND ec.recheck_status='pending'
                   ON CONFLICT (id) DO NOTHING""", (case_id,),
            )

    def claim(self, worker_id: str) -> dict[str, Any] | None:
        with psycopg.connect(self.dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT * FROM knowledge_enrichment_jobs
                   WHERE (status = 'queued' AND (next_retry_at IS NULL OR next_retry_at <= now()))
                      OR (status = 'running' AND lease_expires_at < now())
                   ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"""
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                """UPDATE knowledge_enrichment_jobs SET status='running', worker_id=%s,
                   attempt_count=attempt_count + 1,
                   lease_expires_at=now() + interval '15 minutes', updated_at=now()
                   WHERE id=%s""", (worker_id, row["id"]),
            )
            return dict(row)

    def transition(
        self, job_id: str, *, status: str, source: SourceRecord | None = None,
        error: str | None = None, retry: bool = False,
    ) -> None:
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE knowledge_enrichment_jobs SET status=%s, source_json=%s, source_id=%s,
                   error=%s, worker_id=NULL, lease_expires_at=NULL,
                   next_retry_at=CASE WHEN %s THEN now() + interval '5 minutes' ELSE NULL END,
                   updated_at=now()
                   WHERE id=%s""",
                (status, Jsonb(source.model_dump(mode="json")) if source else None,
                 source.source_id if source else None, error, retry, job_id),
            )
            if status in {"published", "unchanged"}:
                cur.execute(
                    """UPDATE knowledge_enrichment_cases SET recheck_status='pending',
                       notification_status='pending'
                       WHERE job_id=%s AND material=true AND recheck_status='waiting_source'""",
                    (job_id,),
                )
                cur.execute(
                    """INSERT INTO case_events
                       (id, case_id, event_type, payload_json)
                       SELECT 'evt_' || md5(ec.job_id || ':' || ec.case_id),
                              ec.case_id, 'knowledge_recheck_pending',
                              jsonb_build_object('job_id', ec.job_id, 'source_id', %s::text)
                       FROM knowledge_enrichment_cases ec
                       WHERE ec.job_id=%s AND ec.recheck_status='pending'
                       ON CONFLICT (id) DO NOTHING""",
                    (source.source_id if source else None, job_id),
                )

    def list_jobs(self, *, status: str | None = None) -> list[dict[str, Any]]:
        with psycopg.connect(self.dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT id, url, title, status, source_json, source_id, error,
                          raw_sha256, parsed_excerpt, draft_json, published_date,
                          created_at, updated_at FROM knowledge_enrichment_jobs
                   WHERE (%s::text IS NULL OR status=%s) ORDER BY created_at DESC LIMIT 100""",
                (status, status),
            )
            return [dict(row) for row in cur.fetchall()]

    def record_raw(self, job_id: str, *, path: Path, sha256: str, parsed_excerpt: str) -> None:
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE knowledge_enrichment_jobs SET raw_path=%s, raw_sha256=%s,
                   parsed_excerpt=%s, updated_at=now() WHERE id=%s""",
                (str(path), sha256, parsed_excerpt[:6000], job_id),
            )

    def record_draft(self, job_id: str, draft: dict[str, Any]) -> None:
        """The inferred source, its evidence and the editions it supersedes, for the reviewer."""
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE knowledge_enrichment_jobs SET draft_json=%s, updated_at=now() WHERE id=%s",
                (Jsonb(draft), job_id),
            )

    def raw_file(self, job_id: str, *, corpus: Path) -> Path:
        with psycopg.connect(self.dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT raw_path FROM knowledge_enrichment_jobs WHERE id=%s",
                (job_id,),
            )
            row = cur.fetchone()
        if row is None or not row["raw_path"]:
            raise KeyError(job_id)
        path = Path(row["raw_path"]).resolve()
        staging = (corpus / ".knowledge_enrichment_staging").resolve()
        if not path.is_relative_to(staging) or not path.is_file():
            raise KeyError(job_id)
        return path

    def approve(self, job_id: str, source: SourceRecord) -> None:
        if source.source_id != f"legal_{job_id.removeprefix('enrich_')}":
            raise ValueError("待审来源必须使用任务生成的版本 ID")
        if source.citation_role == "primary_legal_basis" and source.authority not in {
            "national_law", "administrative_regulation", "departmental_rule", "judicial_interpretation",
        }:
            raise ValueError("政策、问答和指南不能以主要法律依据发布")
        require_consistent_metadata(source)
        with psycopg.connect(self.dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT url FROM knowledge_enrichment_jobs WHERE id=%s AND status='awaiting_review'
                   FOR UPDATE""", (job_id,),
            )
            row = cur.fetchone()
            if row is None or canonical_url(row["url"]) != canonical_url(source.source_url):
                raise ValueError("待审任务不存在或来源 URL 不匹配")
            cur.execute(
                """UPDATE knowledge_enrichment_jobs SET status='approved', source_json=%s,
                   updated_at=now() WHERE id=%s""",
                (Jsonb(source.model_dump(mode="json")), job_id),
            )

    def reject(self, job_id: str) -> None:
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE knowledge_enrichment_jobs SET status='rejected', updated_at=now()
                   WHERE id=%s AND status='awaiting_review'""", (job_id,),
            )
            if cur.rowcount != 1:
                raise ValueError("待审任务不存在")

    def retry(self, job_id: str) -> None:
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE knowledge_enrichment_jobs SET status='queued', error=NULL,
                   next_retry_at=NULL, updated_at=now()
                   WHERE id=%s AND status='failed'""", (job_id,),
            )
            if cur.rowcount != 1:
                raise ValueError("失败任务不存在")

    def case_rechecks(self, case_id: str) -> list[dict[str, Any]]:
        with psycopg.connect(self.dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT j.id AS job_id, j.title, j.url, j.source_id, j.status AS source_status,
                          ec.material, ec.recheck_status, ec.notification_status,
                          ec.notification_message_id, ec.created_at
                   FROM knowledge_enrichment_cases ec
                   JOIN knowledge_enrichment_jobs j ON j.id=ec.job_id
                   WHERE ec.case_id=%s ORDER BY ec.created_at DESC""", (case_id,),
            )
            return [dict(row) for row in cur.fetchall()]

    def claim_notifications(self) -> list[dict[str, Any]]:
        with psycopg.connect(self.dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT ec.job_id, ec.case_id, j.title, j.url
                   FROM knowledge_enrichment_cases ec
                   JOIN knowledge_enrichment_jobs j ON j.id=ec.job_id
                   WHERE (
                     ec.notification_status='pending'
                     AND (ec.notification_retry_at IS NULL OR ec.notification_retry_at <= now())
                   ) OR (
                     ec.notification_status='sending'
                     AND ec.notification_lease_expires_at < now()
                   )
                   ORDER BY ec.created_at FOR UPDATE OF ec SKIP LOCKED LIMIT 20"""
            )
            rows = [dict(row) for row in cur.fetchall()]
            for row in rows:
                cur.execute(
                    """UPDATE knowledge_enrichment_cases SET notification_status='sending',
                       notification_lease_expires_at=now() + interval '2 minutes'
                       WHERE job_id=%s AND case_id=%s""",
                    (row["job_id"], row["case_id"]),
                )
            return rows

    def mark_notification(
        self, job_id: str, case_id: str, *, sent: bool,
        error: str | None = None, message_id: str | None = None,
    ) -> None:
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE knowledge_enrichment_cases SET notification_status=%s,
                   notification_error=%s,
                   notification_message_id=COALESCE(%s, notification_message_id),
                   notification_retry_at=CASE WHEN %s THEN NULL ELSE now() + interval '5 minutes' END,
                   notification_lease_expires_at=NULL
                   WHERE job_id=%s AND case_id=%s""",
                ("sent" if sent else "pending", error, message_id, sent, job_id, case_id),
            )

    def claim_approved(self, worker_id: str) -> dict[str, Any] | None:
        with psycopg.connect(self.dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT * FROM knowledge_enrichment_jobs WHERE status='approved'
                   ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 1"""
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                """UPDATE knowledge_enrichment_jobs SET status='running', worker_id=%s,
                   attempt_count=attempt_count + 1,
                   lease_expires_at=now() + interval '15 minutes' WHERE id=%s""",
                (worker_id, row["id"]),
            )
            return dict(row)


def _tls_contexts() -> list[ssl.SSLContext]:
    """The default handshake, then the two that older government servers accept, all verifying certificates.

    Some provincial sites answer the default key exchange with a malformed curve point (BAD_ECPOINT)
    or drop it; they complete it when offered only P-256, or only TLS 1.2 with RSA key exchange."""

    p256 = ssl.create_default_context()
    p256.set_ecdh_curve("prime256v1")
    rsa = ssl.create_default_context()
    rsa.maximum_version = ssl.TLSVersion.TLSv1_2
    rsa.set_ciphers("AES128-GCM-SHA256:AES256-GCM-SHA384:AES128-SHA:AES256-SHA")
    return [ssl.create_default_context(), p256, rsa]


def _open(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "LawAgent/0.1"})
    contexts = _tls_contexts() if urlsplit(url).scheme == "https" else [None]
    for index, context in enumerate(contexts):
        try:
            return urllib.request.urlopen(request, timeout=30, context=context)
        except urllib.error.URLError as exc:
            if index == len(contexts) - 1 or not isinstance(exc.reason, ssl.SSLError):
                raise
    raise AssertionError("unreachable")


def _download(url: str, path: Path) -> None:
    if not is_official_url(url):
        raise ValueError("来源不在官方域名范围")
    with _open(url) as response:
        if not is_official_url(response.url):
            raise ValueError("官方来源重定向至非受信地址")
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError("官方文件超过下载上限")
    path.write_bytes(data)


def _has_clause_structure(prepared: PreparedSource) -> bool:
    """Whether a formal instrument yields real clause structure when chunked.

    The old gate counted article headings in the raw text and required 500
    characters. That rejected short genuine instruments and, worse, described
    nothing about what would actually reach the index. A clause number on a
    published chunk is the same evidence ingestion and citation governance use.
    """

    return any(chunk.article_no for chunk in prepared.chunks)


def review_source_id(job_id: str) -> str:
    return f"legal_{job_id.removeprefix('enrich_')}"


def _chunk_samples(prepared: PreparedSource) -> list[dict[str, str]]:
    """A few chunks spread over the document, so the reviewer sees how it will be split."""

    chunks = prepared.chunks
    step = max(1, len(chunks) // DRAFT_CHUNK_SAMPLES)
    return [{"heading": " / ".join(chunk.heading_path[1:]), "text": chunk.text[:240]}
            for chunk in chunks[::step][:DRAFT_CHUNK_SAMPLES]]


def _draft(inference: SourceInference, prepared: PreparedSource, editions: list[SourceRecord],
           problem: str | None, attachments: list[tuple[str, str]] = ()) -> dict[str, Any]:
    return {
        "page_kind": inference.page_kind,
        "attachments": [{"name": name, "url": url, "follow": number in inference.follow}
                        for number, (name, url) in enumerate(attachments, start=1)],
        "source": inference.source.model_dump(mode="json"),
        "evidence": inference.evidence,
        "unresolved": inference.unresolved,
        "supersedes": [{"source_id": item.source_id, "title": item.title, "date": version_date(item)}
                       for item in editions],
        "supersession_problem": problem,
        "chunk_count": len(prepared.chunks),
        "chunks": _chunk_samples(prepared),
        "publish_blocked": prepared.blocks_publish,
        "problems": metadata_problems(inference.source),
        "reader": inference.reader,
    }


def _publish(source: SourceRecord, path: Path, editions: list[SourceRecord], *, job: dict[str, Any],
             store: PostgresEnrichmentStore, service: KnowledgeBaseAdminService, impact) -> None:
    """Publish ``source`` and retire the editions it supersedes, ending each where it takes over."""

    if impact is None:
        service.ingest_file(source, path)
    else:
        impact.track_publication(service, source.source_id, lambda: service.ingest_file(source, path),
                                 job_id=job["id"])
    for retired in retire_editions(source, editions):
        service.update_source_metadata(retired)
    store.transition(job["id"], status="published", source=source)


def process_enrichment_job(
    job: dict[str, Any], *, store: PostgresEnrichmentStore,
    service: KnowledgeBaseAdminService, client, impact=None,
) -> None:
    """Download, parse and publish one discovered official source, or hand it to a reviewer prefilled.

    The model reads the metadata from the document (:func:`read_source_metadata`; ``client`` is
    the model client, and with None or a failed call the rules prefill the draft instead). An
    enacted norm the model read with nothing uncertain, consistent metadata, a clean parse, real
    clause structure and, if it is a new edition, a date after the one it replaces, publishes on
    its own; a rules draft never does. Anything else waits for review with
    the draft, the evidence for each field, sample chunks and the editions it would supersede,
    so the reviewer confirms or corrects rather than fills in. An approved job publishes the
    reviewer's record with the same edition handling.
    """

    try:
        suffix = Path(urlsplit(job["url"]).path).suffix.lower()
        if suffix not in {".pdf", ".docx", ".txt", ".html", ".htm"}:
            suffix = ".html"
        staging = service.corpus / ".knowledge_enrichment_staging" / job["id"]
        staging.mkdir(parents=True, exist_ok=True)
        path = Path(job["raw_path"]) if job.get("raw_path") else staging / f"source{suffix}"
        if not path.resolve().is_relative_to(staging.resolve()):
            raise ValueError("待审原件路径不在任务目录内")
        if job.get("raw_sha256") and path.exists():
            actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual_hash != job["raw_sha256"]:
                raise ValueError("待审原件哈希与已保存记录不一致")
        else:
            _download(job["url"], path)
            header = path.read_bytes()[:8]
            actual_suffix = ".pdf" if header.startswith(b"%PDF") else ".docx" if header.startswith(b"PK\x03\x04") else suffix
            if actual_suffix != path.suffix:
                renamed = path.with_suffix(actual_suffix)
                path.replace(renamed)
                path = renamed
        # The page's own title names the document, so cleaning can drop the "<title>_site" line and chrome.
        parsed = parse_document(path, parser="auto", source=provisional_source(path).model_copy(
            update={"title": clean_title(job["title"])}))
        document = prepare_document_for_ingest(path, parser="auto", parsed=parsed)
        if not document.text.strip():
            raise ValueError("官方文件未能解析出正文")
        raw_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        store.record_raw(
            job["id"], path=path, sha256=raw_hash, parsed_excerpt=document.text,
        )
        source_file_format = path.suffix.lstrip(".")
        attachments: list[tuple[str, str]] = []
        inference: SourceInference | None = None
        if not job.get("source_json"):
            # One reading of the page: its metadata, and whether it only issues attached files.
            if path.suffix in {".html", ".htm"}:
                attachments = attachment_links(_decode_html(path.read_bytes()), job["url"])
            inference = read_source_metadata(
                client=client, source_id=review_source_id(job["id"]), title=job["title"], text=document.text,
                raw_text=parsed.text, url=job["url"], source_site=host_of(job["url"]),
                file_format=source_file_format, published_hint=job.get("published_date"),
                context=job.get("context_text"), attachments=attachments,
            )
            if inference.page_kind == "issuing_notice" and "page_kind" not in inference.unresolved:
                # The page only issues attached files: index the ones chosen, never the notice.
                chosen = [attachments[number - 1] for number in inference.follow]
                usable = [(attachment_title(name), url) for name, url in chosen
                          if Path(urlsplit(url).path).suffix.lower() in PARSEABLE_ATTACHMENTS and is_official_url(url)]
                if not usable:
                    raise ValueError("页面只是印发通知，正文在附件中，" + (
                        "但选中的附件不是可解析的官方文件：" + "；".join(f"{name}（{url}）" for name, url in chosen)
                        if chosen else "但没有可入库的附件（页面上没有附件链接，或附件都不需要入库），请人工核对"))
                store.expand(job["id"], attachments=usable, context_text=document.text,
                             published_date=job.get("published_date"))
                return
            trimmed_body = trim_web_body(
                document.text,
                start_quote=inference.body_start_quote,
                end_quote=inference.body_end_quote,
                file_format=source_file_format,
            )
            if trimmed_body != document.text:
                document = document.model_copy(update={"text": trimmed_body})
        published = [item.source for item in service.list_sources() if item.source.library_kind == "legal"]
        same_url = [item for item in published if canonical_url(item.source_url) == canonical_url(job["url"])]
        content_hash = normalized_content_hash(document.text)
        matching = next(
            (item for item in same_url if service.get_source(item.source_id).get("content_hash") == content_hash),
            None,
        )
        if matching is not None:
            # An identical body does not make the published source current:
            # the chunking, the processing signature and the source metadata are
            # part of the same duplicate rule. Asking that rule keeps a source
            # that merely matches the stored hash from being reported as
            # unchanged while its index is stale, and republishes it instead.
            candidate = prepare_bound_document(document, matching)
            if service.is_up_to_date(matching, document.text, candidate.chunks):
                store.transition(job["id"], status="unchanged", source=matching)
                return

        if job.get("source_json"):
            # Approved by a reviewer: the record is theirs; editions are still matched and retired.
            source = SourceRecord.model_validate(job["source_json"]).model_copy(
                update={"file_format": source_file_format})
            editions = [*{item.source_id: item for item in [*same_url, *previous_editions(source, published)]
                          if item.source_id != source.source_id}.values()]
            prepare_bound_document(document, source).require_publishable()
            _publish(source, path, editions, job=job, store=store, service=service, impact=impact)
            return

        assert inference is not None
        source = inference.source
        if same_url:
            # A changed body at a known address is a new edition of what is published there.
            source = source.model_copy(update={"instrument_key": same_url[0].instrument_key or same_url[0].title})
        editions = [*{item.source_id: item for item in [*same_url, *previous_editions(source, published)]}.values()]
        problem = supersession_problem(source, editions) if editions else None
        prepared = prepare_bound_document(document, source)
        inference = replace(inference, source=source)
        store.record_draft(job["id"], _draft(inference, prepared, editions, problem, attachments))
        auto = (inference.settled and source.doc_type in AUTO_PUBLISH_DOC_TYPES and problem is None
                and not metadata_problems(source)
                and not prepared.blocks_publish and _has_clause_structure(prepared))
        if not auto:
            store.transition(job["id"], status="awaiting_review", error=problem)
            return
        _publish(source, path, editions, job=job, store=store, service=service, impact=impact)
    except Exception as exc:  # noqa: BLE001 - persist every worker failure
        transient = isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))
        retry = transient and int(job.get("attempt_count") or 0) < 2
        store.transition(
            job["id"], status="queued" if retry else "failed",
            error=str(exc)[:2000], retry=retry,
        )
