"""Package, verify and restore a legal corpus using its existing vectors only."""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from law_agent.config import load_service_config
from law_agent.kb.service import InMemoryIndex, KnowledgeBase
from law_agent.review.retrieval.corpus import load_corpus

DEFAULT_CORPUS = ROOT / "data/corpus/legal_docs_20260702"
MANIFEST = "transfer_manifest.json"


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def cached_vectors(corpus: Path, dimension: int):
    chunks = load_corpus(corpus / "chunks.jsonl")
    if not chunks or len({chunk.chunk_id for chunk in chunks}) != len(chunks):
        raise ValueError("Corpus must contain unique, nonempty chunks")
    state = json.loads((corpus / ".knowledge_base_state.json").read_text(encoding="utf-8"))
    vectors = {}
    for chunk in chunks:
        source = state["sources"][chunk.source_id]
        if source.get("status") != "ready" or not source.get("generation_id"):
            raise ValueError(f"Source is not ready: {chunk.source_id}")
        kb = KnowledgeBase(corpus, index=InMemoryIndex(), signature=source["signature"])
        entry = state["embedding_cache"].get(kb._embedding_key(chunk))
        if entry is None or entry.get("signature") != source["signature"]:
            raise ValueError(f"Missing compatible cached vector: {chunk.chunk_id}")
        vector = entry["vector"]
        if len(vector) != dimension or any(
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) for value in vector
        ):
            raise ValueError(f"Invalid cached vector: {chunk.chunk_id}")
        vectors[chunk.chunk_id] = vector
    return chunks, state, vectors


def pack(corpus: Path, output: Path) -> dict:
    corpus = corpus.resolve()
    relative = corpus.relative_to(ROOT)
    config = load_service_config()
    chunks, state, _ = cached_vectors(corpus, config.embedding.dimension)
    paths = [corpus / name for name in (
        "chunks.jsonl", "source_manifest.csv", ".knowledge_base_state.json",
    )]
    if (corpus / "retrieval_cases.jsonl").exists():
        paths.append(corpus / "retrieval_cases.jsonl")
    paths.extend(sorted(path for path in (corpus / "raw").rglob("*") if path.is_file()))
    files = []
    for path in paths:
        if path.is_symlink() or not path.resolve().is_relative_to(corpus):
            raise ValueError(f"File escapes corpus: {path}")
        files.append({"path": path.relative_to(corpus).as_posix(),
                      "bytes": path.stat().st_size, "sha256": sha256(path)})
    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "code_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True,
        ).strip(),
        "corpus": relative.as_posix(), "chunks": len(chunks),
        "sources": len({chunk.source_id for chunk in chunks}),
        "embedding_model": config.embedding.model,
        "embedding_dimension": config.embedding.dimension,
        "source_signatures": sorted({state["sources"][c.source_id]["signature"] for c in chunks}),
        "files": files,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for entry, path in zip(files, paths, strict=True):
            archive.write(path, (relative / entry["path"]).as_posix())
        archive.writestr((relative / MANIFEST).as_posix(),
                         json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise ValueError("ZIP integrity check failed")
        for entry in files:
            with archive.open((relative / entry["path"]).as_posix()) as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != entry["sha256"]:
                    raise ValueError(f"Corpus changed during packaging: {entry['path']}")
    digest = sha256(output)
    output.with_suffix(output.suffix + ".sha256").write_text(
        f"{digest}  {output.name}\n", encoding="utf-8",
    )
    return {"archive": str(output), "bytes": output.stat().st_size, "sha256": digest,
            "chunks": manifest["chunks"], "sources": manifest["sources"], "model_calls": 0}


def verify(corpus: Path):
    corpus = corpus.resolve()
    manifest = json.loads((corpus / MANIFEST).read_text(encoding="utf-8"))
    listed = set()
    for entry in manifest["files"]:
        relative = Path(entry["path"])
        path = (corpus / relative).resolve()
        if relative.is_absolute() or not path.is_relative_to(corpus) or path in listed:
            raise ValueError(f"Invalid manifest path: {relative}")
        listed.add(path)
        if path.stat().st_size != entry["bytes"] or sha256(path) != entry["sha256"]:
            raise ValueError(f"File checksum mismatch: {relative}")
    required = {corpus / name for name in (
        "chunks.jsonl", "source_manifest.csv", ".knowledge_base_state.json",
    )}
    if not required <= listed:
        raise ValueError("Manifest omits required corpus files")
    chunks, state, vectors = cached_vectors(corpus, manifest["embedding_dimension"])
    if len(chunks) != manifest["chunks"] or len({c.source_id for c in chunks}) != manifest["sources"]:
        raise ValueError("Corpus counts do not match manifest")
    return manifest, chunks, state, vectors


def restore(corpus: Path, *, execute: bool = False) -> dict:
    manifest, chunks, state, vectors = verify(corpus)
    summary = {"chunks": len(chunks), "sources": manifest["sources"],
               "embedding_model": manifest["embedding_model"], "model_calls": 0,
               "executed": execute}
    if not execute:
        return summary
    from law_agent.kb.service_index import ServiceGenerationIndex

    config = load_service_config()
    if (config.embedding.model, config.embedding.dimension) != (
        manifest["embedding_model"], manifest["embedding_dimension"],
    ):
        raise ValueError("Configured embedding model/dimension differs from the package")
    index = ServiceGenerationIndex(config)
    try:
        count = int(index.es.count(index=config.elasticsearch.index_name).get("count", 0))
        with index.pg.cursor() as cursor:
            cursor.execute(f"SELECT COUNT(*) FROM {config.postgres.table_name}")
            pg_count = cursor.fetchone()[0]
        if count or pg_count:
            raise ValueError("Restore requires empty ES and pgvector indexes; existing data was not replaced")
        grouped = defaultdict(list)
        for chunk in chunks:
            grouped[chunk.source_id].append(chunk)
        for source_id, source_chunks in grouped.items():
            generation = state["sources"][source_id]["generation_id"]
            index.stage(source_id, generation, source_chunks, vectors)
            index.verify(source_id, generation, {c.chunk_id for c in source_chunks})
            index.activate(source_id, generation)
            print(f"Restored {source_id}: {len(source_chunks)} chunks", flush=True)
    finally:
        index.close()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["pack", "verify", "restore"])
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if args.command == "pack":
        if args.output is None:
            parser.error("pack requires --output")
        result = pack(args.corpus, args.output)
    else:
        result = restore(args.corpus, execute=args.command == "restore" and args.execute)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
