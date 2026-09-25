"""Persistent, fail-closed official-source enrichment."""

from __future__ import annotations

import hashlib
import re
import urllib.error
import urllib.request
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from law_agent.data.chunking.law import split_law_article_sections
from law_agent.data.schemas import SourceRecord
from law_agent.kb.admin import KnowledgeBaseAdminService
from law_agent.kb.ingestion import prepare_document_for_ingest
from law_agent.review.web_research import (
    WebFinding,
    canonical_url,
    host_of,
    is_trusted_official_url,
)

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
DATE_PATTERN = re.compile(r"自\s*(20\d{2})年(\d{1,2})月(\d{1,2})日\s*起施行")
ORDER_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,20}(?:部|委员会|办公室)第[一二三四五六七八九十百千\d]+号令")
DEPARTMENT_ISSUERS = {
    "moj.gov.cn": ("司法部", ("司法部令", "司法部第")),
    "cac.gov.cn": ("国家互联网信息办公室", ("国家互联网信息办公室令", "国家互联网信息办公室第")),
    "miit.gov.cn": ("工业和信息化部", ("工业和信息化部令", "工业和信息化部第")),
    "mnr.gov.cn": ("自然资源部", ("自然资源部令", "自然资源部第")),
}


def verified_auto_source(title: str, text: str, url: str) -> SourceRecord | None:
    """Only unambiguous enactments with visible effective dates may publish."""
    if not is_trusted_official_url(url) or title not in text[:1200]:
        return None
    match = DATE_PATTERN.search(text)
    if match is None:
        return None
    try:
        effective = date(*(int(part) for part in match.groups()))
    except ValueError:
        return None
    host = host_of(url)
    authority: str
    doc_type: str
    issuer: str
    if host == "flk.npc.gov.cn" and title.startswith("中华人民共和国") and title.endswith("法"):
        authority, doc_type, issuer = "national_law", "law", "全国人民代表大会及其常务委员会"
    elif host in {"sousuo.www.gov.cn", "xzfg.moj.gov.cn"} and title.endswith("条例") and "国务院令" in text[:1200]:
        authority, doc_type, issuer = "administrative_regulation", "regulation", "国务院"
    elif (
        host in DEPARTMENT_ISSUERS
        and ORDER_PATTERN.search(text[:1200])
        and any(token in text[:1200] for token in DEPARTMENT_ISSUERS[host][1])
    ):
        authority, doc_type, issuer = "departmental_rule", "regulation", DEPARTMENT_ISSUERS[host][0]
    else:
        return None
    digest = hashlib.sha256((canonical_url(url) + "\x1f" + text).encode("utf-8")).hexdigest()[:20]
    instrument_key = hashlib.sha256(title.strip().encode("utf-8")).hexdigest()[:20]
    return SourceRecord(
        source_id=f"legal_{digest}", title=title, source_url=url,
        source_site=host, doc_type=doc_type, authority=authority,
        citation_role="primary_legal_basis",
        law_status="effective" if effective <= datetime.now(UTC).date() else "not_yet_effective",
        effective_date=effective.isoformat(), issuing_body=issuer,
        instrument_key=instrument_key,
        file_format=Path(urlsplit(url).path).suffix.lstrip(".") or "html",
    )


class PostgresEnrichmentStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def enqueue(self, finding: WebFinding, *, case_id: str, review_task_id: str) -> str | None:
        if (finding.known_source_id and not finding.refresh_needed) or not is_trusted_official_url(finding.url):
            return None
        key = canonical_url(finding.url)
        candidate_hash = hashlib.sha256(
            ((finding.published_date or "") + "\x1f" + finding.excerpt).encode("utf-8")
        ).hexdigest()
        with psycopg.connect(self.dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """INSERT INTO knowledge_enrichment_jobs
                   (id, canonical_url, candidate_hash, url, title, excerpt, published_date)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (canonical_url, candidate_hash) DO UPDATE SET updated_at = now()
                   RETURNING id""",
                (f"enrich_{uuid4().hex}", key, candidate_hash, finding.url, finding.title, finding.excerpt, finding.published_date),
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

    def mark_impact(self, *, case_id: str, review_task_id: str, urls: list[str]) -> None:
        if not urls:
            return
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE knowledge_enrichment_cases ec SET material = true,
                       recheck_status = CASE WHEN j.status = 'published' THEN 'pending' ELSE 'waiting_source' END,
                       notification_status = CASE WHEN j.status = 'published' THEN 'pending' ELSE 'not_required' END
                   FROM knowledge_enrichment_jobs j
                   WHERE ec.job_id = j.id AND ec.case_id = %s AND ec.review_task_id = %s
                     AND ec.recheck_status IN ('not_required', 'waiting_source')
                     AND j.canonical_url = ANY(%s)""",
                (case_id, review_task_id, [canonical_url(url) for url in urls]),
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
            if status == "published":
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
                              jsonb_build_object('job_id', ec.job_id, 'source_id', %s)
                       FROM knowledge_enrichment_cases ec
                       WHERE ec.job_id=%s AND ec.recheck_status='pending'
                       ON CONFLICT (id) DO NOTHING""",
                    (source.source_id if source else None, job_id),
                )

    def list_jobs(self, *, status: str | None = None) -> list[dict[str, Any]]:
        with psycopg.connect(self.dsn) as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """SELECT id, url, title, status, source_json, source_id, error,
                          raw_sha256, parsed_excerpt,
                          created_at, updated_at FROM knowledge_enrichment_jobs
                   WHERE (%s IS NULL OR status=%s) ORDER BY created_at DESC LIMIT 100""",
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
            "national_law", "administrative_regulation", "departmental_rule",
        }:
            raise ValueError("政策、问答和指南不能以主要法律依据发布")
        if not source.effective_date and source.citation_role == "primary_legal_basis":
            raise ValueError("正式法规须填写经核实的生效日期")
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


def _download(url: str, path: Path) -> None:
    if not is_trusted_official_url(url):
        raise ValueError("来源不在官方域名范围")
    request = urllib.request.Request(url, headers={"User-Agent": "LawAgent/0.1"})
    with urllib.request.urlopen(request, timeout=30) as response:
        if not is_trusted_official_url(response.url):
            raise ValueError("官方来源重定向至非受信地址")
        data = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(data) > MAX_DOWNLOAD_BYTES:
        raise ValueError("官方文件超过下载上限")
    path.write_bytes(data)


def process_enrichment_job(
    job: dict[str, Any], *, store: PostgresEnrichmentStore,
    service: KnowledgeBaseAdminService,
) -> None:
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
        document = prepare_document_for_ingest(path, parser="auto")
        if not document.text.strip():
            raise ValueError("官方文件未能解析出正文")
        raw_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        store.record_raw(
            job["id"], path=path, sha256=raw_hash, parsed_excerpt=document.text,
        )
        source = (
            SourceRecord.model_validate(job["source_json"])
            if job.get("source_json") else
            verified_auto_source(job["title"], document.text, job["url"])
        )
        if (
            source is not None and not job.get("source_json")
            and (len(document.text) < 500 or len(split_law_article_sections(document.text)) < 3)
        ):
            source = None
        if source is None:
            store.transition(job["id"], status="awaiting_review")
            return
        if source.citation_role == "primary_legal_basis" and (
            len(document.text) < 500 or len(split_law_article_sections(document.text)) < 3
        ):
            raise ValueError("正式法源缺少可核对的完整条款结构")
        source = source.model_copy(update={"file_format": path.suffix.lstrip(".")})
        service.ingest_file(source, path)
        store.transition(job["id"], status="published", source=source)
    except Exception as exc:  # noqa: BLE001 - persist every worker failure
        transient = isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError))
        retry = transient and int(job.get("attempt_count") or 0) < 2
        store.transition(
            job["id"], status="queued" if retry else "failed",
            error=str(exc)[:2000], retry=retry,
        )
