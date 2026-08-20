"""Web-facing knowledge-base administration primitives.

The HTTP layer uses these helpers for read-only source inspection and for the
same source-aware ingest/remove operations exposed by the maintenance CLI.
No shell command is spawned here: the safety boundary remains
``KnowledgeBase`` and ``ServiceGenerationIndex``.
"""

from __future__ import annotations

import json
import shutil
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4

from law_agent.config import require_service_config
from law_agent.data.chunking.pipeline import chunk_document
from law_agent.data.schemas import SourceRecord
from law_agent.kb.ingestion import prepare_document_for_ingest
from law_agent.kb.service import (
    InMemoryIndex,
    KnowledgeBase,
    SourceSummary,
    processing_signature,
)
from law_agent.kb.service_index import ServiceGenerationIndex
from law_agent.llm.embeddings import build_embeddings_provider

LibraryKind = Literal["legal", "internal_policy"]
JobStatus = Literal["queued", "running", "succeeded", "partially_succeeded", "failed"]


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class KnowledgeJob:
    id: str
    job_type: Literal["import", "delete", "restore", "metadata"]
    status: JobStatus = "queued"
    payload: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    created_by: str = ""
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)


class KnowledgeJobStore(Protocol):
    def initialize(self) -> None: ...

    def create(self, job_type: str, payload: dict[str, Any], created_by: str) -> KnowledgeJob: ...

    def update(self, job_id: str, **changes: Any) -> KnowledgeJob: ...

    def get(self, job_id: str) -> KnowledgeJob | None: ...


class InMemoryKnowledgeJobStore:
    """Thread-safe store used by API tests and local development."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.jobs: dict[str, KnowledgeJob] = {}

    def initialize(self) -> None:
        return None

    def create(self, job_type: str, payload: dict[str, Any], created_by: str) -> KnowledgeJob:
        with self._lock:
            job = KnowledgeJob(
                id=f"kbjob_{uuid4().hex[:16]}",
                job_type=job_type,  # type: ignore[arg-type]
                payload=json.loads(json.dumps(payload, ensure_ascii=False)),
                created_by=created_by,
            )
            self.jobs[job.id] = job
            return job

    def update(self, job_id: str, **changes: Any) -> KnowledgeJob:
        with self._lock:
            job = self.jobs[job_id]
            for key, value in changes.items():
                setattr(job, key, value)
            job.updated_at = utc_now()
            return job

    def get(self, job_id: str) -> KnowledgeJob | None:
        with self._lock:
            return self.jobs.get(job_id)


@dataclass(frozen=True)
class TrashRecord:
    source_id: str
    library_kind: LibraryKind
    title: str
    archive_dir: str
    trashed_at: str
    expires_at: str
    source: dict[str, Any]


class KnowledgeBaseAdminService:
    """Source listing plus safe web operations over one corpus directory."""

    def __init__(self, corpus: Path | str, *, read_only: bool = False) -> None:
        self.corpus = Path(corpus)
        self.read_only = read_only
        self.trash_root = self.corpus / ".knowledge_base_trash"
        self._mutation_lock = threading.RLock()

    def _read_kb(self) -> KnowledgeBase:
        return KnowledgeBase(self.corpus, index=InMemoryIndex())

    def list_sources(
        self,
        *,
        library_kind: LibraryKind | None = None,
        query: str = "",
        status: str | None = None,
    ) -> list[SourceSummary]:
        needle = query.strip().casefold()
        records = self._read_kb().list_sources()
        items: list[SourceSummary] = []
        for item in records:
            source = item.source
            if library_kind and source.library_kind != library_kind:
                continue
            if status and (source.internal_status if source.library_kind == "internal_policy" else source.law_status) != status:
                continue
            if needle and needle not in f"{source.title} {source.source_id} {source.issuing_body or ''} {source.owning_department or ''}".casefold():
                continue
            items.append(item)
        return items

    def get_source(self, source_id: str) -> dict[str, Any]:
        summaries = {item.source.source_id: item for item in self._read_kb().list_sources()}
        summary = summaries.get(source_id)
        if summary is None:
            raise KeyError(source_id)
        source = summary.source
        raw_dir = self.corpus / "raw" / source_id
        raw_files = sorted(raw_dir.glob("source.*"))
        raw_file = raw_files[0] if raw_files else None
        state = self._read_state().get("sources", {}).get(source_id, {})
        return {
            **asdict(summary),
            "source": source.model_dump(mode="json"),
            "raw_filename": raw_file.name if raw_file else None,
            "raw_size": raw_file.stat().st_size if raw_file and raw_file.exists() else None,
            "raw_path": str(raw_file) if raw_file else None,
            "content_hash": state.get("content_hash"),
            "generation_id": state.get("generation_id"),
        }

    def raw_path(self, source_id: str) -> Path:
        detail = self.get_source(source_id)
        path = detail.get("raw_path")
        if not path:
            raise KeyError(source_id)
        return Path(path)

    def ingest_file(self, source: SourceRecord, file_path: Path, *, parser: str = "auto") -> dict[str, Any]:
        if self.read_only:
            raise RuntimeError("知识库当前为只读挂载")
        with self._mutation_lock:
            document = prepare_document_for_ingest(file_path, parser=parser)
            final_document = document.model_copy(
                update={
                    "doc_id": source.source_id,
                    "source_id": source.source_id,
                    "library_kind": source.library_kind,
                    "title": source.title,
                    "source_url": source.source_url,
                    "source_site": source.source_site,
                    "doc_type": source.doc_type,
                    "authority": source.authority,
                    "law_status": source.law_status,
                    "publish_date": source.publish_date,
                    "effective_date": source.effective_date,
                    "issuing_body": source.issuing_body,
                    "owning_department": source.owning_department,
                    "internal_status": source.internal_status,
                    "applicable_region": source.applicable_region,
                    "legal_domain": source.legal_domain,
                    "applicable_subjects": source.applicable_subjects,
                    "topic_tags": source.topic_tags,
                }
            )
            chunks = chunk_document(final_document)
            config = require_service_config()
            index = ServiceGenerationIndex(config)
            try:
                embeddings = build_embeddings_provider(config.embedding)
                kb = KnowledgeBase(
                    self.corpus,
                    index=index,
                    signature=processing_signature(
                        embedding_model=config.embedding.model,
                        embedding_dimension=config.embedding.dimension,
                    ),
                    embed_texts=embeddings.embed_texts,
                )
                result = kb.ingest_prepared(
                    source,
                    final_document.text,
                    chunks,
                    raw_file=file_path,
                )
            finally:
                index.close()
            return {
                "action": result.action,
                "source_id": result.source_id,
                "generation_id": result.generation_id,
                "embedded_chunks": result.embedded_chunks,
                "cached_chunks": result.cached_chunks,
            }

    def trash_source(self, source_id: str) -> TrashRecord:
        if self.read_only:
            raise RuntimeError("知识库当前为只读挂载")
        with self._mutation_lock:
            detail = self.get_source(source_id)
            source = SourceRecord.model_validate(detail["source"])
            raw_path = Path(detail["raw_path"]) if detail.get("raw_path") else None
            trash_id = f"{source_id}_{uuid4().hex[:8]}"
            archive = self.trash_root / trash_id
            archive.mkdir(parents=True, exist_ok=False)
            (archive / "source.json").write_text(
                json.dumps(source.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            if raw_path and raw_path.exists():
                shutil.copy2(raw_path, archive / raw_path.name)
            trashed_at = datetime.now(UTC)
            record = TrashRecord(
                source_id=source_id,
                library_kind=source.library_kind,
                title=source.title,
                archive_dir=str(archive),
                trashed_at=trashed_at.isoformat(),
                expires_at=(trashed_at + timedelta(days=30)).isoformat(),
                source=source.model_dump(mode="json"),
            )
            (archive / "trash.json").write_text(
                json.dumps(asdict(record), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            config = require_service_config()
            index = ServiceGenerationIndex(config)
            try:
                KnowledgeBase(self.corpus, index=index).remove_source(source_id)
            except Exception:
                shutil.rmtree(archive, ignore_errors=True)
                raise
            finally:
                index.close()
            return record

    def list_trash(self, library_kind: LibraryKind | None = None) -> list[dict[str, Any]]:
        if not self.trash_root.exists():
            return []
        items: list[dict[str, Any]] = []
        for path in sorted(self.trash_root.iterdir()):
            record_path = path / "trash.json"
            if not record_path.exists():
                continue
            record = json.loads(record_path.read_text(encoding="utf-8"))
            if library_kind and record.get("library_kind") != library_kind:
                continue
            items.append(record)
        return items

    def restore_source(self, source_id: str) -> dict[str, Any]:
        records = [item for item in self.list_trash() if item["source_id"] == source_id]
        if not records:
            raise KeyError(source_id)
        record = records[-1]
        archive = Path(record["archive_dir"])
        source = SourceRecord.model_validate(record["source"])
        raw_files = [item for item in archive.glob("source.*") if item.name != "source.json"]
        if not raw_files:
            raise RuntimeError("回收站中没有可恢复的原文件")
        result = self.ingest_file(source, raw_files[0])
        shutil.rmtree(archive, ignore_errors=True)
        return {"source_id": source_id, "action": "restored", "ingest": result}

    def _read_state(self) -> dict[str, Any]:
        path = self.corpus / ".knowledge_base_state.json"
        if not path.exists():
            return {"sources": {}}
        return json.loads(path.read_text(encoding="utf-8"))


def source_summary_payload(item: SourceSummary) -> dict[str, Any]:
    source = item.source
    return {
        "source": source.model_dump(mode="json"),
        "chunk_count": item.chunk_count,
        "status": item.status,
        "raw_format": item.raw_format,
    }
