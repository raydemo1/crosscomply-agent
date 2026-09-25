"""Shared source parsing boundary for CLI and web knowledge administration."""

from __future__ import annotations

import hashlib
from pathlib import Path

from law_agent.data.chunking.pipeline import chunk_document
from law_agent.data.cleaners.pipeline import clean_document
from law_agent.data.normalize import normalize_source
from law_agent.data.quality import QualityResult, evaluate_chunks, evaluate_text
from law_agent.data.schemas import Chunk, Document, SourceRecord


class ParseQualityError(RuntimeError):
    """Raised when a parse is still visibly corrupted and must not be published.

    This is the gate that keeps parser artifacts (spaced-out identifiers,
    decode corruption, leftover contents leaders) out of the index. It runs at
    ingestion only: retrieval must never have to re-judge a body that was
    already refused or already accepted.
    """

    def __init__(self, source_id: str, result: QualityResult) -> None:
        self.source_id = source_id
        self.result = result
        super().__init__(f"拒绝入库 {source_id}：解析质量未达标（{result.describe()}）")


def _infer_title(path: Path) -> str:
    if path.suffix.lower() in {".txt", ".md", ".markdown", ".html", ".htm"}:
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            candidate = line.lstrip("# ").strip()
            if candidate:
                return candidate[:120]
    return path.stem.replace("_", " ")


def provisional_source(path: Path) -> SourceRecord:
    return SourceRecord(
        source_id="candidate_"
        + hashlib.sha256(str(path.resolve()).encode("utf-8")).hexdigest()[:12],
        title=_infer_title(path),
        source_url=path.resolve().as_uri(),
        source_site="local_import",
        doc_type="guideline",
        file_format=path.suffix.lstrip(".") or "txt",
        include_in_mvp=True,
    )


def record_quality(document: Document, quality: QualityResult) -> Document:
    """Attach the document-gate verdict to the ingest provenance."""

    return document.model_copy(
        update={
            "ingest_meta": document.ingest_meta.model_copy(
                update={"quality_status": quality.status, "quality_issues": quality.codes}
            )
        }
    )


def prepare_document_for_ingest(path: Path, *, parser: str) -> Document:
    """Parse, clean and quality-gate one ingest request.

    A body that is still damaged after every parser has been tried is refused
    here rather than published and filtered later.
    """

    document = clean_document(normalize_source(provisional_source(path), path, parser=parser))
    # ``min_chars=1``: brevity is a routing signal (a short extracted "text
    # layer" is what sends a PDF to OCR), not damage. A genuinely short
    # guideline is publishable; only artifacts or an empty extraction block it.
    quality = evaluate_text(document.text, min_chars=1)
    if quality.status == "fail":
        raise ParseQualityError(document.doc_id, quality)
    return record_quality(document, quality)


def prepare_chunks_for_publish(document: Document) -> list[Chunk]:
    """Chunk a document that passed the document gate, then quality-gate the chunks.

    Chunking sits between the two gates because damage can survive cleaning in
    a single clause while the document as a whole still reads healthy; grading
    each chunk keeps a clean majority from masking it.
    """

    chunks = chunk_document(document)
    quality = evaluate_chunks(chunk.text for chunk in chunks)
    if quality.status == "fail":
        raise ParseQualityError(document.doc_id, quality)
    return chunks


__all__ = [
    "ParseQualityError",
    "prepare_chunks_for_publish",
    "prepare_document_for_ingest",
    "provisional_source",
    "record_quality",
]