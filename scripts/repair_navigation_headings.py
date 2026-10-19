"""Preview or publish content-preserving repairs of heading parents and boundaries."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from law_agent.config import require_service_config
from law_agent.data.chunking.structured import _heading_from_line, _push_heading
from law_agent.data.io import read_jsonl, read_manifest
from law_agent.data.schemas import Chunk
from law_agent.kb.service import InMemoryIndex, KnowledgeBase, make_stable_chunks
from law_agent.kb.service_index import ServiceGenerationIndex
from law_agent.llm.embeddings import build_embeddings_provider
from scripts.analyze_navigation import file_hash, offline_tools


def repair_paths(chunks: list[Chunk]) -> list[Chunk]:
    stack = []
    document = None
    repaired = []
    for chunk in sorted(chunks, key=lambda c: (c.doc_id, c.chunk_index, c.chunk_id)):
        if chunk.doc_id != document:
            stack = []
            document = chunk.doc_id
        path = chunk.heading_path
        if path and path[0] == chunk.title:
            path = path[1:]
        if not path:
            stack = []
        for part in path:
            heading = _heading_from_line(part)
            if heading is None:
                stack = list(path)
                break
            stack = _push_heading(stack, *heading)
        updated = [chunk.title, *(part for part in stack if part)]
        repaired.append(chunk.model_copy(update={"heading_path": updated, "citation_label": " ".join(updated)}))
    return repaired


def move_trailing_heading(chunks: list[Chunk], chunk_index: int) -> list[Chunk]:
    updated = list(chunks)
    position = next(i for i, chunk in enumerate(updated) if chunk.chunk_index == chunk_index)
    left, right = updated[position:position + 2]
    body, separator, heading = left.text.rpartition("\n")
    parsed = _heading_from_line(heading)
    if not separator or not body or not parsed or parsed[0] != 1 or left.doc_id != right.doc_id:
        raise ValueError("Expected a trailing top-level heading and following chunk in the same document")
    path = right.heading_path[1:] if right.heading_path[:1] == [right.title] else right.heading_path
    updated[position] = left.model_copy(update={"text": body, "char_count": len(body)})
    text = heading + "\n" + right.text
    updated[position + 1] = right.model_copy(update={
        "text": text, "char_count": len(text), "heading_path": [right.title, heading, *path],
    })
    if "\n".join(c.text for c in updated) != "\n".join(c.text for c in chunks):
        raise ValueError("Boundary move changed source text")
    return updated


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path("data/corpus/legal_docs_20260702"))
    parser.add_argument("--source-id", action="append", required=True)
    parser.add_argument("--chunk-range", action="append", default=[], help="Limit a source repair to source_id:start:end (inclusive)")
    parser.add_argument("--move-trailing-heading", action="append", default=[], help="Move a misplaced trailing heading to the next chunk: source_id:chunk_index")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if len(args.source_id) != len(set(args.source_id)):
        parser.error("Duplicate source")
    ranges = {}
    for value in args.chunk_range:
        source_id, start, end = value.rsplit(":", 2)
        if source_id not in args.source_id or not 0 <= int(start) <= int(end):
            parser.error("Invalid source or chunk range")
        ranges.setdefault(source_id, []).append([int(start), int(end)])
    moves = {}
    for value in args.move_trailing_heading:
        source_id, chunk_index = value.rsplit(":", 1)
        if source_id not in args.source_id or source_id in ranges or int(chunk_index) < 0:
            parser.error("Boundary repair requires a selected, unrestricted source")
        moves.setdefault(source_id, []).append(int(chunk_index))
    chunks = read_jsonl(args.corpus / "chunks.jsonl", Chunk)
    sources = {s.source_id: s for s in read_manifest(args.corpus / "source_manifest.csv")}
    state = json.loads((args.corpus / ".knowledge_base_state.json").read_text(encoding="utf-8"))
    args.output.mkdir(parents=True, exist_ok=True)
    if (args.output / "publication.json").exists():
        raise ValueError("Publication already recorded")
    plans = []
    for source_id in args.source_id:
        source = sources[source_id]
        original = [c for c in chunks if c.source_id == source_id]
        if not original:
            raise ValueError(f"No chunks for {source_id}")
        signature = state["sources"][source_id]["signature"]
        if [c.chunk_id for c in make_stable_chunks(original, source, signature=signature)] != [c.chunk_id for c in original]:
            raise ValueError("Original stable IDs differ")
        shifted = original
        for chunk_index in moves.get(source_id, []):
            shifted = move_trailing_heading(shifted, chunk_index)
        updated = repair_paths(shifted)
        if source_id in ranges:
            updated = [new if any(start <= old.chunk_index <= end for start, end in ranges[source_id]) else old
                       for old, new in zip(original, updated, strict=True)]
        stable = make_stable_chunks(updated, source, signature=signature)
        for old, new in zip(original, stable, strict=True):
            if (old.can_cite_clause, old.citation_role) != (new.can_cite_clause, new.citation_role):
                raise ValueError("Repair changed citation qualification")
        if "\n".join(c.text for c in original) != "\n".join(c.text for c in stable):
            raise ValueError("Repair changed source content")
        probe = KnowledgeBase(args.corpus, index=InMemoryIndex(), signature=signature)
        missing = sum(probe._embedding_key(c) not in state["embedding_cache"] for c in stable)
        if missing > 2 * len(moves.get(source_id, [])):
            raise ValueError("Unexpected missing cached embeddings")
        plans.append((source, original, updated, stable, signature))
    from law_agent.review.schemas import ReviewFacts

    previews = []
    for source, original, updated, stable, signature in plans:
        outline = offline_tools(stable).read_evidence(source.source_id, None, None, ReviewFacts(as_of_date="2026-10-19"))
        previews.append({
            "source_id": source.source_id, "source_chunks": len(original),
            "changed_paths": [{"old_chunk_id": old.chunk_id, "new_chunk_id": new.chunk_id,
                               "old_path": old.heading_path, "new_path": new.heading_path}
                              for old, new in zip(original, stable, strict=True) if old.heading_path != new.heading_path],
            "root_outline": [entry.model_dump(mode="json") for entry in outline.outline],
            "changed_text_chunks": [new.chunk_id for old, new in zip(original, stable, strict=True) if old.text != new.text],
            "source_content_changed": False, "citation_qualification_changed": False,
        })
    preview = {"corpus_before": file_hash(args.corpus / "chunks.jsonl"), "sources": previews, "chunk_ranges": ranges, "boundary_moves": moves}
    preview_path = args.output / "preview.json"
    if preview_path.exists() and json.loads(preview_path.read_text(encoding="utf-8")) != preview:
        raise ValueError("Preview differs from previously frozen repair")
    save(preview_path, preview)
    if not args.execute:
        print(json.dumps({"sources": [{"source_id": p["source_id"], "changes": len(p["changed_paths"])} for p in previews]}))
        return
    backup = args.output / "corpus_before"
    backup.mkdir(exist_ok=False)
    for name in ("chunks.jsonl", "source_manifest.csv", ".knowledge_base_state.json"):
        shutil.copy2(args.corpus / name, backup / name)
    config = require_service_config()
    index = ServiceGenerationIndex(config)
    publications = []
    try:
        for source, original, updated, stable, signature in plans:
            previous = state["sources"][source.source_id]
            index.verify(source.source_id, previous["generation_id"], {c.chunk_id for c in original})
            embedding_limit = 2 * len(moves.get(source.source_id, []))

            def cached_only(texts, limit=embedding_limit):
                if texts:
                    if len(texts) > limit:
                        raise RuntimeError("Unexpected embedding requests")
                    return build_embeddings_provider(config.embedding).embed_texts(texts)
                return []

            kb = KnowledgeBase(args.corpus, index=index, signature=signature, embed_texts=cached_only)
            result = kb._republish(source, previous["content_hash"], updated, stable_chunks=stable)
            index.verify(source.source_id, result.generation_id, {c.chunk_id for c in stable})
            publications.append({"source_id": source.source_id, "generation_id": result.generation_id,
                                 "cached_chunks": result.cached_chunks, "embedded_chunks": result.embedded_chunks})
            save(args.output / "progress.json", publications)
        after = read_jsonl(args.corpus / "chunks.jsonl", Chunk)
        if len(after) != len(chunks) or [c.model_dump() for c in after if c.source_id not in args.source_id] != [c.model_dump() for c in chunks if c.source_id not in args.source_id]:
            raise ValueError("Other sources or total count changed")
        save(args.output / "publication.json", {**preview, "publications": publications,
             "corpus_after": file_hash(args.corpus / "chunks.jsonl"), "other_sources_unchanged": True})
        print(json.dumps(publications))
    finally:
        index.close()


if __name__ == "__main__":
    main()
