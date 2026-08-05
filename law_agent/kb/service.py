"""Deep module for source-aware knowledge-base ingestion.

The public seam is :class:`KnowledgeBase`: callers provide a stable source,
normalized document text and chunks; the module owns duplicate detection,
stable chunk identities, embedding-cache accounting, generation switching and
the corpus artifacts.  Real search stores can implement the small
``GenerationIndex`` protocol; tests use ``InMemoryIndex``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from law_agent.data.io import read_jsonl, read_manifest, write_jsonl, write_manifest
from law_agent.data.schemas import Chunk, SourceRecord
from law_agent.review.retrieval.text import normalize_text

Action = Literal["added", "updated", "skipped_duplicate"]


def normalized_content_hash(text: str) -> str:
    """Return a filename-independent identity for a document body."""

    return hashlib.sha256(normalize_text(text).encode("utf-8")).hexdigest()


def processing_signature(
    *,
    parser_version: str = "0.1.0",
    cleaning_version: str = "0.1.0",
    chunking_version: str = "stable-content-v1",
    embedding_model: str = "unknown",
    embedding_dimension: int = 0,
) -> str:
    """Fingerprint the transformations that make cached vectors compatible."""

    payload = {
        "parser": parser_version,
        "cleaning": cleaning_version,
        "chunking": chunking_version,
        "embedding_model": embedding_model,
        "embedding_dimension": embedding_dimension,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _chunk_locator(chunk: Chunk) -> str:
    return "|".join(
        [
            "/".join(chunk.heading_path),
            chunk.article_no or "",
            chunk.paragraph_no or "",
            chunk.item_no or "",
        ]
    )


def make_stable_chunks(
    chunks: Iterable[Chunk],
    source: SourceRecord,
    *,
    signature: str = "stable-content-v1",
) -> list[Chunk]:
    """Return chunks with IDs stable across filename and ordinal shifts.

    A repeated identical passage under the same structural location receives a
    deterministic occurrence suffix, preserving separate citation locations.
    """

    stable: list[Chunk] = []
    occurrences: dict[str, int] = {}
    for index, chunk in enumerate(chunks):
        material = "\x1f".join(
            (source.source_id, signature, _chunk_locator(chunk), normalize_text(chunk.text))
        )
        occurrence = occurrences.get(material, 0)
        occurrences[material] = occurrence + 1
        digest = hashlib.sha256(f"{material}\x1f{occurrence}".encode()).hexdigest()[:24]
        stable.append(
            chunk.model_copy(
                update={
                    "chunk_id": f"{source.source_id}:{digest}",
                    "doc_id": source.source_id,
                    "source_id": source.source_id,
                    "chunk_index": index,
                }
            )
        )
    return [
        chunk.model_copy(
            update={
                "prev_chunk_id": stable[index - 1].chunk_id if index else None,
                "next_chunk_id": stable[index + 1].chunk_id if index + 1 < len(stable) else None,
            }
        )
        for index, chunk in enumerate(stable)
    ]


class GenerationIndex(Protocol):
    """Small seam for staging, switching and cleaning a source generation."""

    def stage(
        self,
        source_id: str,
        generation_id: str,
        chunks: list[Chunk],
        embeddings: dict[str, list[float]],
    ) -> None: ...

    def verify(self, source_id: str, generation_id: str, expected_ids: set[str]) -> None: ...

    def activate(self, source_id: str, generation_id: str) -> str | None: ...

    def delete_generation(self, source_id: str, generation_id: str) -> None: ...

    def delete_source(self, source_id: str) -> None: ...


class InMemoryIndex:
    """Generation index adapter used by tests and local dry runs."""

    def __init__(self) -> None:
        self.generations: dict[tuple[str, str], list[Chunk]] = {}
        self.current: dict[str, str] = {}

    def stage(
        self,
        source_id: str,
        generation_id: str,
        chunks: list[Chunk],
        embeddings: dict[str, list[float]],
    ) -> None:
        self.generations[(source_id, generation_id)] = list(chunks)

    def verify(self, source_id: str, generation_id: str, expected_ids: set[str]) -> None:
        actual = {chunk.chunk_id for chunk in self.generations[(source_id, generation_id)]}
        if actual != expected_ids:
            raise RuntimeError("staged index chunk IDs do not match expected chunks")

    def activate(self, source_id: str, generation_id: str) -> str | None:
        previous = self.current.get(source_id)
        self.current[source_id] = generation_id
        return previous

    def delete_generation(self, source_id: str, generation_id: str) -> None:
        self.generations.pop((source_id, generation_id), None)

    def delete_source(self, source_id: str) -> None:
        for key in [key for key in self.generations if key[0] == source_id]:
            self.generations.pop(key, None)
        self.current.pop(source_id, None)


@dataclass(frozen=True)
class IngestResult:
    action: Action
    source_id: str
    generation_id: str | None
    embedded_chunks: int
    cached_chunks: int


@dataclass(frozen=True)
class SourceSummary:
    """User-facing state for one active knowledge-base source."""

    source: SourceRecord
    chunk_count: int
    status: str
    raw_format: str


class KnowledgeBase:
    """Own corpus state for one directory behind a compact ingestion interface."""

    def __init__(
        self,
        corpus_root: Path | str,
        *,
        index: GenerationIndex,
        signature: str = "stable-content-v1",
        embed_texts: Callable[[Sequence[str]], list[list[float]]] | None = None,
    ) -> None:
        self.root = Path(corpus_root)
        self.index = index
        self.signature = signature
        self.embed_texts = embed_texts or (lambda texts: [[0.0] for _ in texts])
        self.manifest_path = self.root / "source_manifest.csv"
        self.chunks_path = self.root / "chunks.jsonl"
        self.state_path = self.root / ".knowledge_base_state.json"

    def ingest_prepared(
        self,
        source: SourceRecord,
        normalized_text: str,
        chunks: list[Chunk],
        *,
        raw_file: Path | None = None,
    ) -> IngestResult:
        """Safely replace one source after parsing and chunking have succeeded."""

        body_hash = normalized_content_hash(normalized_text)
        state = self._read_state()
        source_state = state["sources"].get(source.source_id)
        if source_state and source_state["content_hash"] == body_hash and source_state["signature"] == self.signature:
            return IngestResult("skipped_duplicate", source.source_id, None, 0, len(chunks))

        stable_chunks = make_stable_chunks(chunks, source, signature=self.signature)
        generation_id = uuid4().hex
        staged_raw = self._stage_canonical_raw(source, raw_file) if raw_file is not None else None
        cache = state["embedding_cache"]
        cache_keys = [self._embedding_key(chunk) for chunk in stable_chunks]
        missing_positions = [index for index, key in enumerate(cache_keys) if key not in cache]
        missing_vectors = self.embed_texts(
            [self._embedding_text(stable_chunks[index]) for index in missing_positions]
        )
        if len(missing_vectors) != len(missing_positions):
            raise RuntimeError("embedding provider returned an unexpected vector count")
        for index, vector in zip(missing_positions, missing_vectors, strict=True):
            cache[cache_keys[index]] = {"signature": self.signature, "vector": vector}
        embeddings = {
            chunk.chunk_id: list(cache[cache_keys[index]]["vector"])
            for index, chunk in enumerate(stable_chunks)
        }
        cached = len(stable_chunks) - len(missing_positions)

        # Stage and verify before current-state artifacts or retrieval change.
        self.index.stage(source.source_id, generation_id, stable_chunks, embeddings)
        self.index.verify(source.source_id, generation_id, {chunk.chunk_id for chunk in stable_chunks})
        old_generation = self.index.activate(source.source_id, generation_id)

        try:
            self._replace_current_artifacts(source, stable_chunks)
            state["sources"][source.source_id] = {
                "content_hash": body_hash,
                "signature": self.signature,
                "generation_id": generation_id,
                "status": "ready",
            }
            self._write_state(state)
            if staged_raw is not None:
                self._activate_staged_raw(staged_raw)
        except Exception:
            # The index has not lost its old generation yet. Revert the
            # pointer so a local artifact failure cannot expose the new state.
            if old_generation is not None:
                self.index.activate(source.source_id, old_generation)
            self.index.delete_generation(source.source_id, generation_id)
            if staged_raw is not None:
                staged_raw.unlink(missing_ok=True)
            raise

        if old_generation is not None:
            try:
                self.index.delete_generation(source.source_id, old_generation)
            except Exception as exc:  # noqa: BLE001 - cleanup must be retried for any store fault
                state["sources"][source.source_id]["cleanup_pending"] = old_generation
                state["sources"][source.source_id]["cleanup_error"] = str(exc)
                self._write_state(state)

        return IngestResult(
            "updated" if source_state else "added",
            source.source_id,
            generation_id,
            len(stable_chunks) - cached,
            cached,
        )

    def _replace_current_artifacts(self, source: SourceRecord, chunks: list[Chunk]) -> None:
        existing_sources = self._read_sources()
        by_id = {record.source_id: record for record in existing_sources}
        by_id[source.source_id] = source
        write_manifest(self.manifest_path, [by_id[key] for key in sorted(by_id)])

        existing_chunks = read_jsonl(self.chunks_path, Chunk)
        retained = [chunk for chunk in existing_chunks if chunk.source_id != source.source_id]
        write_jsonl(self.chunks_path, [*retained, *chunks])

    def _read_sources(self) -> list[SourceRecord]:
        if not self.manifest_path.exists():
            return []
        return read_manifest(self.manifest_path)

    def list_sources(self) -> list[SourceSummary]:
        """Return active sources with the information needed for a readable list."""

        counts: dict[str, int] = {}
        for chunk in read_jsonl(self.chunks_path, Chunk):
            counts[chunk.source_id] = counts.get(chunk.source_id, 0) + 1
        state = self._read_state()
        return [
            SourceSummary(
                source=source,
                chunk_count=counts.get(source.source_id, 0),
                status=str(state["sources"].get(source.source_id, {}).get("status", "unknown")),
                raw_format=self._raw_format(source),
            )
            for source in sorted(self._read_sources(), key=lambda item: (item.title, item.source_id))
        ]

    def _raw_format(self, source: SourceRecord) -> str:
        raw_dir = self.root / "raw" / source.source_id
        raw_files = sorted(raw_dir.glob("source.*")) if raw_dir.exists() else []
        return raw_files[0].suffix.lstrip(".") if raw_files else source.file_format

    def remove_source(self, source_id: str) -> SourceSummary:
        """Permanently remove one source from retrieval and active corpus artifacts.

        The caller is responsible for an explicit user confirmation. Retrieval
        stores are cleared first so a source is never left searchable after a
        successful removal response.
        """

        summaries = {summary.source.source_id: summary for summary in self.list_sources()}
        summary = summaries.get(source_id)
        if summary is None:
            raise RuntimeError(f"未找到来源：{source_id}")

        self.index.delete_source(source_id)
        write_manifest(
            self.manifest_path,
            [source for source in self._read_sources() if source.source_id != source_id],
        )
        write_jsonl(
            self.chunks_path,
            [chunk for chunk in read_jsonl(self.chunks_path, Chunk) if chunk.source_id != source_id],
        )

        state = self._read_state()
        state["sources"].pop(source_id, None)
        self._write_state(state)

        raw_dir = self.root / "raw" / source_id
        if raw_dir.exists():
            import shutil

            shutil.rmtree(raw_dir)
        return summary

    def exact_matches(self, normalized_text: str) -> list[SourceRecord]:
        """Find sources with the same normalized body, independent of filename."""

        digest = normalized_content_hash(normalized_text)
        state = self._read_state()
        matching_ids = {
            source_id
            for source_id, source_state in state["sources"].items()
            if source_state.get("content_hash") == digest
        }
        return [source for source in self._read_sources() if source.source_id in matching_ids]

    def title_candidates(self, title: str) -> list[SourceRecord]:
        """Return conservative same-title candidates for user confirmation."""

        normalized_title = normalize_text(title)
        return [
            source
            for source in self._read_sources()
            if normalize_text(source.title) == normalized_title
        ]

    def _read_state(self) -> dict[str, dict]:
        if not self.state_path.exists():
            return {"sources": {}, "embedding_cache": {}}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def _write_state(self, state: dict[str, dict]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.state_path)

    def _embedding_key(self, chunk: Chunk) -> str:
        material = "\x1f".join((self.signature, normalize_text(self._embedding_text(chunk))))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _embedding_text(chunk: Chunk) -> str:
        return f"{chunk.title}\n{chunk.text}" if chunk.title else chunk.text

    def _stage_canonical_raw(self, source: SourceRecord, raw_file: Path) -> Path:
        """Copy candidate raw data without replacing the current source yet."""

        import shutil

        target_dir = self.root / "raw" / source.source_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"source{raw_file.suffix.lower()}"
        temporary = target.with_suffix(f"{target.suffix}.{uuid4().hex}.tmp")
        shutil.copy2(raw_file, temporary)
        return temporary

    def _activate_staged_raw(self, temporary: Path) -> None:
        target = Path(str(temporary).rsplit(".", 2)[0])
        temporary.replace(target)
        target_dir = target.parent
        for sibling in target_dir.glob("source.*"):
            if sibling != target:
                sibling.unlink()
