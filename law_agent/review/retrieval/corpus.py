"""Corpus loading for review retrieval.

Issue 5: Load the cleaned Phase 1 corpus chunks so that review cases can
retrieve evidence from the real 42-document cleaned corpus.
"""

from __future__ import annotations

from pathlib import Path

from law_agent.data.io import read_jsonl
from law_agent.data.schemas import Chunk

DEFAULT_CHUNKS_PATH = Path("data/corpus/legal_docs_20260702/chunks.jsonl")


class CorpusError(RuntimeError):
    """Raised when the corpus cannot be loaded."""


def load_corpus(chunks_path: Path | str = DEFAULT_CHUNKS_PATH) -> list[Chunk]:
    """Load cleaned corpus chunks from a JSONL file.

    Raises ``CorpusError`` with a clear message when the file is missing or
    contains schema-invalid records, so the CLI can surface a readable error
    instead of a raw traceback.
    """

    path = Path(chunks_path)
    if not path.exists():
        raise CorpusError(f"chunks file does not exist: {path}")
    if not path.is_file():
        raise CorpusError(f"chunks path is not a file: {path}")

    try:
        chunks = read_jsonl(path, Chunk)
    except Exception as exc:
        raise CorpusError(f"failed to load chunks from {path}: {exc}") from exc

    if not chunks:
        raise CorpusError(f"chunks file is empty: {path}")
    return chunks
