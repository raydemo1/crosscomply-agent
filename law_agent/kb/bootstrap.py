"""One-time migration of the checked-in legacy corpus into KB layout."""

from __future__ import annotations

import csv
import hashlib
import json
import shutil
from difflib import SequenceMatcher
from pathlib import Path

from law_agent.data.io import read_jsonl, write_jsonl, write_manifest
from law_agent.data.schemas import Chunk, Document, SourceRecord
from law_agent.kb.service import make_stable_chunks, normalized_content_hash
from law_agent.review.retrieval.text import normalize_text

LEGACY_MANIFESTS = (
    "source_manifest.review.csv",
    "cac_sources.review.csv",
    "additional_non_law_sources.review.csv",
)


def _read_legacy_manifest(path: Path) -> list[SourceRecord]:
    fields = set(SourceRecord.model_fields)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [
            SourceRecord.model_validate({key: value for key, value in row.items() if key in fields})
            for row in csv.DictReader(handle)
        ]


def load_legacy_sources(corpus_root: Path) -> list[SourceRecord]:
    """Merge the three authoritative legacy manifests with duplicate checks."""

    sources: dict[str, SourceRecord] = {}
    for name in LEGACY_MANIFESTS:
        path = corpus_root / name
        if not path.exists():
            continue
        for source in _read_legacy_manifest(path):
            existing = sources.get(source.source_id)
            if existing is not None and existing != source:
                raise RuntimeError(f"conflicting metadata for source_id {source.source_id}")
            sources[source.source_id] = source
    if not sources:
        raise RuntimeError("no legacy source manifests found")
    return [sources[key] for key in sorted(sources)]


def _read_legacy_documents(path: Path) -> list[Document]:
    """Read older normalized JSONL that may contain retired trace fields."""

    fields = set(Document.model_fields)
    documents: list[Document] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                payload = json.loads(line)
                documents.append(Document.model_validate({key: value for key, value in payload.items() if key in fields}))
    return documents


def _legacy_raw_mapping(corpus_root: Path, source_ids: set[str]) -> dict[str, Path]:
    """Resolve every source with fetch_status's recorded filename first."""

    raw_root = corpus_root / "raw"
    all_raw = [path for path in raw_root.rglob("*") if path.is_file()]
    by_name: dict[str, list[Path]] = {}
    for path in all_raw:
        by_name.setdefault(path.name, []).append(path)

    mapping: dict[str, Path] = {}
    # A completed local initialization is safe to resume when the online
    # index build was interrupted: canonical raws are already authoritative.
    for source_id in source_ids:
        canonical = list((raw_root / source_id).glob("source.*"))
        if len(canonical) == 1:
            mapping[source_id] = canonical[0]
    status_path = corpus_root / "fetch_status.csv"
    if status_path.exists():
        with status_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                source_id = row.get("source_id") or ""
                if source_id not in source_ids or row.get("ok", "").lower() != "true":
                    continue
                candidates = by_name.get(Path(row.get("path") or "").name, [])
                if len(candidates) == 1:
                    mapping[source_id] = candidates[0]

    source_by_id = {source.source_id: source for source in load_legacy_sources(corpus_root)}
    for source_id in source_ids - set(mapping):
        candidates = [
            path for path in all_raw
            if path.stem == source_id or path.name.startswith(f"{source_id}.")
        ]
        if len(candidates) == 1:
            mapping[source_id] = candidates[0]

    # Hand-curated legacy files use numeric prefixes (for example
    # ``001_汽车数据安全管理若干规定.md``) instead of source IDs.  The numeric
    # suffix is a stronger fact than a fuzzy title; title similarity only
    # disambiguates duplicate raw copies.  Equal-byte duplicates are safe to
    # select because the extra file remains untouched as an unreferenced raw.
    for source_id in source_ids - set(mapping):
        source = source_by_id[source_id]
        number = source_id.rsplit("_", 1)[-1]
        candidates = [
            path for path in all_raw
            if path.name.startswith(f"{number}_") or path.name.startswith(f"{number}-")
        ]
        if candidates:
            ranked = sorted(
                candidates,
                key=lambda path: SequenceMatcher(
                    None, normalize_text(source.title), normalize_text(path.stem)
                ).ratio(),
                reverse=True,
            )
            if len(ranked) == 1:
                mapping[source_id] = ranked[0]
            else:
                first, second = ranked[:2]
                first_score = SequenceMatcher(None, normalize_text(source.title), normalize_text(first.stem)).ratio()
                second_score = SequenceMatcher(None, normalize_text(source.title), normalize_text(second.stem)).ratio()
                same_bytes = hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
                if first_score - second_score >= 0.05 or same_bytes:
                    mapping[source_id] = first

    # One standard document has a descriptive source ID but no numeric prefix.
    for source_id in source_ids - set(mapping):
        source = source_by_id[source_id]
        numeric_tokens = [token for token in normalize_text(source.title).split() if token.isdigit()]
        candidates = [
            path for path in all_raw
            if any(token in normalize_text(path.name) for token in numeric_tokens)
        ]
        if len(candidates) == 1:
            mapping[source_id] = candidates[0]

    unresolved = sorted(source_ids - set(mapping))
    if unresolved:
        raise RuntimeError(f"cannot uniquely map raw files for: {', '.join(unresolved)}")
    return mapping


def initialize_legacy_corpus(corpus_root: Path | str, *, signature: str) -> dict[str, int]:
    """Move source raws, merge manifests and rekey existing chunks.

    The caller is responsible for rebuilding ES and pgvector immediately after
    this function.  It refuses ambiguous raw-file mappings rather than moving
    a file based on a title guess.
    """

    root = Path(corpus_root)
    sources = load_legacy_sources(root)
    source_by_id = {source.source_id: source for source in sources}
    raw_mapping = _legacy_raw_mapping(root, set(source_by_id))
    documents = _read_legacy_documents(root / "documents.normalized.jsonl")
    documents_by_id = {document.source_id: document for document in documents}
    if set(documents_by_id) != set(source_by_id):
        raise RuntimeError("legacy manifests and normalized documents do not describe the same sources")

    legacy_chunks = read_jsonl(root / "chunks.jsonl", Chunk)
    chunks_by_source: dict[str, list[Chunk]] = {source_id: [] for source_id in source_by_id}
    for chunk in legacy_chunks:
        try:
            chunks_by_source[chunk.source_id].append(chunk)
        except KeyError as exc:
            raise RuntimeError(f"chunk references unknown source {chunk.source_id}") from exc

    rewritten: list[Chunk] = []
    for source_id in sorted(source_by_id):
        rewritten.extend(
            make_stable_chunks(chunks_by_source[source_id], source_by_id[source_id], signature=signature)
        )

    # Validate all moves before changing a single source directory.
    moves: list[tuple[Path, Path]] = []
    for source_id, raw_path in raw_mapping.items():
        target = root / "raw" / source_id / f"source{raw_path.suffix.lower()}"
        if raw_path != target:
            moves.append((raw_path, target))

    for raw_path, target in moves:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and hashlib.sha256(target.read_bytes()).digest() != hashlib.sha256(raw_path.read_bytes()).digest():
            raise RuntimeError(f"canonical raw target conflicts with {raw_path}")
    for raw_path, target in moves:
        if target.exists():
            raw_path.unlink()
        else:
            shutil.move(str(raw_path), str(target))

    write_manifest(root / "source_manifest.csv", sources)
    write_jsonl(root / "chunks.jsonl", rewritten)
    state = {
        "sources": {
            source_id: {
                "content_hash": normalized_content_hash(documents_by_id[source_id].text),
                "signature": signature,
                "generation_id": None,
                "status": "ready",
            }
            for source_id in sorted(source_by_id)
        },
        "embedding_cache": {},
    }
    (root / ".knowledge_base_state.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {"sources": len(sources), "chunks": len(rewritten), "raw_moves": len(moves)}
