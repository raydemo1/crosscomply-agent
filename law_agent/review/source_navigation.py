"""Deterministic, date-scoped navigation over governed source chunks."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date

from law_agent.data.schemas import Chunk, StrictModel


class EvidenceLocation(StrictModel):
    node_id: str
    parent_node_id: str | None
    title: str
    heading_path: list[str]
    child_count: int
    chunk_count: int
    article_no: str | None
    chunk_id: str
    preview: str


@dataclass
class SourceNode:
    node_id: str
    parent_node_id: str | None
    title: str
    heading_path: list[str]
    article_no: str | None = None
    children: list[str] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    direct_chunk_ids: list[str] = field(default_factory=list)

    def location(self) -> EvidenceLocation:
        return EvidenceLocation(
            node_id=self.node_id, parent_node_id=self.parent_node_id,
            title=self.title, heading_path=self.heading_path,
            child_count=len(self.children), chunk_count=len(self.chunks),
            article_no=self.article_no, chunk_id=self.chunks[0].chunk_id,
            preview=self.chunks[0].text[:180],
        )


class SourceNavigation:
    def __init__(self, source_chunks: list[Chunk], available: list[Chunk], as_of: date):
        self.nodes: dict[str, SourceNode] = {}
        self.roots: list[str] = []
        ordered = sorted(source_chunks, key=lambda c: (c.doc_id, c.chunk_index, c.chunk_id))
        fingerprint = hashlib.sha256(json.dumps(
            [c.model_dump(mode="json") for c in ordered],
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        active: list[SourceNode] = []
        last_doc: str | None = None
        for chunk in sorted(available, key=lambda c: (c.doc_id, c.chunk_index, c.chunk_id)):
            path = [part.strip() for part in chunk.heading_path if part.strip()]
            while path and path[0] == chunk.title.strip():
                path.pop(0)
            if chunk.article_no and chunk.article_no not in path:
                path.append(chunk.article_no)
            if not path:
                path = [f"正文片段 {chunk.chunk_index + 1}"]
            common = 0
            if last_doc == chunk.doc_id:
                while (common < min(len(active), len(path))
                       and active[common].title == path[common]):
                    common += 1
            active = active[:common]
            for depth in range(common, len(path)):
                identity = [fingerprint, as_of.isoformat(), chunk.doc_id,
                            path[:depth + 1], chunk.chunk_index, chunk.chunk_id]
                node_id = "nav_" + hashlib.sha256(json.dumps(
                    identity, ensure_ascii=False, separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
                parent = active[-1] if active else None
                node = SourceNode(
                    node_id=node_id, parent_node_id=parent.node_id if parent else None,
                    title=path[depth], heading_path=path[:depth + 1],
                    article_no=chunk.article_no if path[depth] == chunk.article_no else None,
                )
                self.nodes[node_id] = node
                (parent.children if parent else self.roots).append(node_id)
                active.append(node)
            for node in active:
                node.chunks.append(chunk)
            active[-1].direct_chunk_ids.append(chunk.chunk_id)
            last_doc = chunk.doc_id

    def node(self, node_id: str) -> SourceNode:
        if node_id not in self.nodes:
            raise ValueError("节点不属于此来源、已失效或不适用于当前审查日期；请重新浏览来源目录")
        return self.nodes[node_id]

    def children(self, parent_node_id: str | None) -> list[EvidenceLocation]:
        ids = self.node(parent_node_id).children if parent_node_id else self.roots
        return [self.nodes[node_id].location() for node_id in ids]
