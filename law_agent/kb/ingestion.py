"""Shared source parsing boundary for CLI and web knowledge administration."""

from __future__ import annotations

import hashlib
from pathlib import Path

from law_agent.data.cleaners.pipeline import clean_document
from law_agent.data.normalize import normalize_source
from law_agent.data.schemas import SourceRecord


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


def prepare_document_for_ingest(path: Path, *, parser: str):
    """Run the mandatory parse-and-clean step for every ingest request."""

    return clean_document(normalize_source(provisional_source(path), path, parser=parser))


__all__ = ["prepare_document_for_ingest", "provisional_source"]
