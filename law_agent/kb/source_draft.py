"""One draft of a new source from its file, shared by the CLI and the web import.

Parse once, have the model read the metadata from the document
(:func:`read_source_metadata`, the rules standing in when no model is
available), let any field the operator stated explicitly win, match the
editions it would supersede, and prepare the chunks with the result — so a file
arrives at the publish gate prefilled, with what is uncertain marked, instead of
"guideline / unknown" defaults that someone has to correct afterwards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from law_agent.data.schemas import SourceRecord
from law_agent.data.source_rules import metadata_problems
from law_agent.kb.ingestion import (
    PreparedSource,
    parse_document,
    prepare_bound_document,
    prepare_document_for_ingest,
)
from law_agent.kb.source_inference import (
    previous_editions,
    retire_editions,
    supersession_problem,
    version_date,
)
from law_agent.kb.source_llm import read_source_metadata

# With these stated by the operator, there is nothing left for the model to classify.
OPERATOR_FIELDS = ("doc_type", "authority", "citation_role", "issuing_body")


@dataclass(frozen=True)
class SourceDraft:
    source: SourceRecord
    prepared: PreparedSource
    evidence: dict[str, str] = field(default_factory=dict)
    unresolved: list[str] = field(default_factory=list)
    editions: list[SourceRecord] = field(default_factory=list)
    supersession_problem: str | None = None
    reader: str = "operator"  # who read the metadata: "model", "rules" (model unavailable) or "operator"

    @property
    def needs_review(self) -> bool:
        """A person must confirm it: a field is uncertain, or only the fallback rules read it."""
        return bool(self.unresolved) or self.reader == "rules"

    @property
    def retired(self) -> list[SourceRecord]:
        """The superseded editions as they are republished once this one is in."""
        return retire_editions(self.source, self.editions)

    def summary(self) -> dict[str, Any]:
        return {
            "evidence": self.evidence,
            "unresolved": self.unresolved,
            "supersedes": [{"source_id": item.source_id, "title": item.title, "date": version_date(item)}
                           for item in self.editions],
            "supersession_problem": self.supersession_problem,
            "problems": metadata_problems(self.source),
            "reader": self.reader,
        }


def draft_source(
    path: Path, *, base: SourceRecord, explicit: dict[str, Any], published: list[SourceRecord],
    client, parser: str = "auto",
) -> SourceDraft:
    """``base`` carries the identity (id, title, url, site); ``explicit`` the fields the operator set,
    which override what is read. ``client`` is the model client, or None to fall back on the rules.
    Internal-policy sources are not read: they are not law."""

    parsed = parse_document(path, parser=parser, source=base)
    document = prepare_document_for_ingest(path, parser=parser, source=base, parsed=parsed)
    stated = {name: value for name, value in explicit.items()
              if name in SourceRecord.model_fields and value not in (None, "", [])}
    evidence: dict[str, str] = {}
    unresolved: list[str] = []
    reader = "operator"
    if base.library_kind != "legal":
        source = base
    elif all(name in stated for name in OPERATOR_FIELDS):
        source = base.model_copy(update=stated)
    else:
        inference = read_source_metadata(
            client=client, source_id=base.source_id, title=base.title, text=document.text, raw_text=parsed.text,
            url=base.source_url, source_site=base.source_site, file_format=base.file_format,
            published_hint=explicit.get("publish_date"),
        )
        source = inference.source.model_copy(update={
            "source_id": base.source_id, "download_url": base.download_url, **stated,
        })
        evidence = {name: reason for name, reason in inference.evidence.items() if name not in stated}
        unresolved = [name for name in inference.unresolved if name not in stated]
        reader = inference.reader
    evidence |= {name: "导入时填写" for name in stated}
    editions = previous_editions(source, published) if source.library_kind == "legal" else []
    return SourceDraft(
        source=source, prepared=prepare_bound_document(document, source), evidence=evidence,
        unresolved=unresolved, editions=editions,
        supersession_problem=supersession_problem(source, editions) if editions else None, reader=reader,
    )
