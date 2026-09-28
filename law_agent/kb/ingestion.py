"""Shared source parsing boundary for CLI and web knowledge administration."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path

from law_agent.data.chunking.pipeline import chunk_document
from law_agent.data.cleaners.common import DOT_LEADER_BROAD_RE
from law_agent.data.cleaners.pipeline import clean_document
from law_agent.data.normalize import normalize_source
from law_agent.data.quality import QualityResult, evaluate_chunks, evaluate_text
from law_agent.data.schemas import Chunk, Document, SourceRecord

# The shortest fragment that counts as evidence a line survives. A shorter run
# is coincidence: without this, a lost sentence could be stitched back together
# one character at a time from characters that occur elsewhere in the corpus.
# The line's own tail is exempt, because a long line the chunker split can end
# in a piece shorter than this.
MIN_COVERAGE_FRAGMENT_CHARS = 8

# Layout-only differences between two renderings of the same line: whitespace,
# table pipes, Markdown emphasis/heading markers and HTML tags. Removed for
# comparison only — the published text is never touched.
_LAYOUT_CHARS_RE = re.compile(r"[\s|#*]")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Everything a line can consist of without holding a character of text.
_LAYOUT_ONLY_LINE_RE = re.compile(r"[\s|:\-]+")
_IMAGE_PLACEHOLDER_RE = re.compile(r"^<!--\s*image\s*-->$", re.IGNORECASE)
# The chunker drops a Markdown-headed reference section; only that form is
# excluded, because a plain "参考文献" line is body text to the chunker as well.
_REFERENCE_HEADING_RE = re.compile(r"^#{1,6}\s+(?:参考文献|references)\s*$", re.IGNORECASE)
# The chunker scopes references by heading path, so that section ends at the next
# heading and anything after it — an appendix, a closing section — is body text
# again and stays under the coverage check.
_MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}\s+\S")
# A section marker a heading line may carry while the heading path keeps only
# the words: "## 2 术语定义" is published as "术语定义". Stripped for comparison
# only, and only when what is left is itself a published heading.
_HEADING_MARKER_RE = re.compile(r"^[#\s]*[A-Za-z]?[\d一二三四五六七八九十百零〇]*(?:[.．][\d]+)*[.．、:：\s]*")
_APPENDED_INTERPRETATION_RE = re.compile(
    r"(?m)^[ \t]*(?:#{1,6}[ \t]*)?《[^\n》]+》[ \t]*解读[ \t]*$"
)


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


@dataclass(frozen=True)
class CoverageGap:
    """A contiguous run of body lines no published chunk carries."""

    line_no: int
    text: str
    chars: int


@dataclass(frozen=True)
class ChunkCoverage:
    """How much of the cleaned body survives into the final chunks.

    Coverage is counted over contiguous text segments, never as a total
    character ratio: a body can lose one whole clause while its total length
    stays close to the original. Body lines are consumed in document order from
    a cursor into the published chunks, so a text that occurs twice is only
    credited once, a body whose order was scrambled is refused, and a line the
    chunker cut across several chunks — with the column header repeated between
    the pieces — still reads as present.

    Deliberate exclusions (a contents block, a layout-only line, an image
    placeholder) are recorded with their reason. Any other line the chunks do
    not carry is a gap, whatever its size: there is no "small enough to ignore"
    threshold, because a deleted exception clause is short by nature.
    """

    total_lines: int
    covered_lines: int
    chunk_count: int
    char_count: int
    missing_chars: int
    missing: list[CoverageGap] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)

    @property
    def ratio(self) -> float:
        return self.covered_lines / self.total_lines if self.total_lines else 1.0

    @property
    def largest_gap_chars(self) -> int:
        return max((gap.chars for gap in self.missing), default=0)

    @property
    def blocks_publish(self) -> bool:
        """Zero chunks gates unconditionally, and so does any unexplained gap."""

        return self.chunk_count == 0 or bool(self.missing)

    def describe(self) -> str:
        detail = (
            f"chunks={self.chunk_count} lines={self.covered_lines}/{self.total_lines}"
            f" chars_missing={self.missing_chars}/{self.char_count}"
            f" largest_gap={self.largest_gap_chars}"
        )
        if self.excluded:
            excluded = ", ".join(f"{key}={value}" for key, value in sorted(self.excluded.items()))
            detail = f"{detail} ({excluded})"
        return detail


class ChunkCoverageError(RuntimeError):
    """Raised when body text never reaches a chunk and must not be published."""

    def __init__(self, source_id: str, coverage: ChunkCoverage) -> None:
        self.source_id = source_id
        self.coverage = coverage
        first = coverage.missing[0] if coverage.missing else None
        where = f"，首个缺失自第 {first.line_no} 行起" if first else ""
        super().__init__(f"拒绝入库 {source_id}：正文未完整进入 Chunk（{coverage.describe()}{where}）")


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


# Every ``SourceRecord`` field a ``Document`` also carries. Deriving the list
# from the two schemas keeps source binding complete by construction: a
# hand-written list silently drops a field that the document schema later
# gains, and the default reaches the chunks as wrong metadata.
_BINDABLE_FIELDS: tuple[str, ...] = tuple(
    name for name in SourceRecord.model_fields if name in Document.model_fields
)


def bind_source(document: Document, source: SourceRecord) -> Document:
    """Attach a governed source's full identity to an already-parsed document.

    Used when the identity is chosen after parsing (the interactive CLI decides
    it from the parsed title). ``normalize_source`` already binds a document it
    parses from a real record; this is the same binding for the two-phase path,
    so both routes carry identical metadata.
    """

    return document.model_copy(
        update={name: getattr(source, name) for name in _BINDABLE_FIELDS}
        | {"doc_id": source.source_id}
    )


def prepare_document_for_ingest(
    path: Path, *, parser: str, source: SourceRecord | None = None
) -> Document:
    """Parse, clean and quality-gate one ingest request.

    A body that is still damaged after every parser has been tried is refused
    here rather than published and filtered later. ``source`` carries the
    governed identity when the caller already knows it; without one a
    provisional record is used, and the caller rebinds later.
    """

    record = source if source is not None else provisional_source(path)
    document = clean_document(normalize_source(record, path, parser=parser))
    if document.doc_type in {"law", "regulation"} and _APPENDED_INTERPRETATION_RE.search(document.text):
        raise ValueError(
            f"拒绝入库 {document.source_id}：法条正文后混入独立解读，"
            "请将正式条文与解读拆成不同来源"
        )
    # ``min_chars=1``: brevity is a routing signal (a short extracted "text
    # layer" is what sends a PDF to OCR), not damage. A genuinely short
    # guideline is publishable; only artifacts or an empty extraction block it.
    quality = evaluate_text(document.text, min_chars=1)
    if quality.status == "fail":
        raise ParseQualityError(document.doc_id, quality)
    return record_quality(document, quality)


def _comparable(text: str) -> str:
    """Reduce a line to its substance so two renderings of it compare equal.

    Comparison only: whitespace, pipes, Markdown markers and tags are added and
    removed by parsers and chunkers without changing what the line says.
    """

    return _LAYOUT_CHARS_RE.sub("", _HTML_TAG_RE.sub("", unescape(text)))


def _exclusion_reason(line: str) -> str | None:
    """Why a body line is expected to be absent from the chunks, if it is."""

    stripped = line.strip()
    if not stripped:
        return None
    if _IMAGE_PLACEHOLDER_RE.match(stripped):
        return "image_placeholder"
    if DOT_LEADER_BROAD_RE.search(stripped):
        return "contents_line"
    if not _LAYOUT_ONLY_LINE_RE.sub("", stripped):
        return "layout_decoration"
    return None


def _match_from(needle: str, corpus: str, index: int) -> int:
    """Length of the longest prefix of ``needle`` the corpus carries at ``index``.

    Presence is monotone in length, so the boundary is binary-searched instead
    of comparing every prefix.
    """

    low, high = 0, len(needle)
    while low < high:
        middle = (low + high + 1) // 2
        if corpus.startswith(needle[:middle], index):
            low = middle
        else:
            high = middle - 1
    return low


def _consume_line(needle: str, corpus: str, cursor: int) -> tuple[int, int]:
    """Consume ``needle`` from ``corpus`` at or after ``cursor``, in order.

    Returns the characters of ``needle`` that no chunk carries and the advanced
    cursor. Matching runs forward only, because the chunks are concatenated in
    document order. That is what lets the check tell *which* copy of a text that
    occurs twice is missing, and notice that a body's order was scrambled: text
    that only occurs earlier in the corpus than the cursor has not survived.

    A line is consumed one fragment at a time rather than as a whole, because
    the chunker may cut it across chunks — a table row longer than a chunk is
    stored as several pieces with the column header repeated between them, so no
    single span of the published text equals the row while the pieces in order
    do.

    Each fragment is consumed at the *earliest* place the line can have
    reached, by preference the place the body itself says it is at: text that
    continues exactly where the previous line stopped is contiguous by
    construction. Only when nothing continues there is the fragment searched
    for further down. A line of a few characters — a table cell the parser
    reduced to ``设备`` — has too little text to anchor on, so searching first
    would match the first of its many copies anywhere later and drag the cursor
    past every line in between, which is exactly the failure this check exists
    to avoid.
    """

    length = len(needle)
    position = 0
    lost = 0
    while position < length:
        rest = needle[position:]
        span = _match_from(rest, corpus, cursor)
        if span < min(len(rest), MIN_COVERAGE_FRAGMENT_CHARS):
            anchor = rest[:MIN_COVERAGE_FRAGMENT_CHARS]
            index = corpus.find(anchor, cursor)
            if index < 0:
                # The rest of the line is in no chunk. A fragment shorter than
                # the anchor is not evidence on its own: characters that short
                # occur everywhere, so stitching a lost line back together from
                # them would credit text no chunk carries.
                return lost + len(rest), cursor
            span = _match_from(rest, corpus, index)
            if span == 0:
                return lost + len(rest), cursor
            cursor = index
        if span == len(rest):
            return lost, cursor + span
        cursor += span
        position += span
    return lost, cursor


def _published_headings(chunks: list[Chunk]) -> set[str]:
    """Every heading the chunks carry as metadata, normalized for comparison.

    Path elements are kept whole and separate. Matching a body line against the
    concatenated paths credited any fragment of a heading: a dropped body line
    reading "第一节" passed as published metadata because "第一节 一般规定" is a
    heading, so a line the chunker never published could escape the check that
    exists to catch it.
    """

    headings: set[str] = set()
    for chunk in chunks:
        for heading in chunk.heading_path:
            comparable = _comparable(heading)
            if comparable:
                headings.add(comparable)
    return headings


def _is_published_heading(needle: str, headings: set[str]) -> bool:
    """True when a body line is a heading the chunks carry as metadata."""

    if needle in headings:
        return True
    words = _HEADING_MARKER_RE.sub("", needle)
    return len(words) >= 2 and words != needle and words in headings


def _published_corpus(chunks: list[Chunk]) -> str:
    """The chunks' text in document order, reduced to its substance.

    Every line of every chunk is kept. A table header the chunker repeats
    between chunks is text the source carries once, but dropping it here to
    save the repetition also dropped the body line that happened to open the
    next chunk whenever the two coincided — the corpus then read as missing
    text the chunks actually hold. Text a chunk adds between two body lines is
    skipped by the in-order consumption instead, which tolerates any layout
    difference without discarding anything.
    """

    return "".join(
        _comparable("\n".join(line for line in chunk.text.splitlines() if line.strip()))
        for chunk in chunks
    )


def check_chunk_coverage(
    document: Document, chunks: list[Chunk], *, max_gap_lines: int = 40
) -> ChunkCoverage:
    """Compare the cleaned body against the chunks it was published as.

    Returns what survived, what did not, and the reason each deliberate
    exclusion was excluded. It judges retention of contiguous text, never a
    character ratio, and never rewrites or re-scores the text: a body that is
    merely re-laid out by the chunker reports full coverage.
    """

    corpus = _published_corpus(chunks)
    # A chapter or section heading is published as chunk metadata rather than as
    # chunk text, so the heading paths are the one other place a body line can
    # survive. Structure is not ordered against body text this way.
    headings = _published_headings(chunks)
    total_lines = covered_lines = char_count = missing_chars = 0
    excluded: dict[str, int] = {}
    missing: list[CoverageGap] = []
    gap: list[tuple[int, str, int]] = []
    gap_chars = 0
    cursor = 0
    in_references = False

    def flush_gap() -> None:
        nonlocal gap, gap_chars
        if gap:
            missing.append(
                CoverageGap(
                    line_no=gap[0][0],
                    text="\n".join(entry[1] for entry in gap[:max_gap_lines]),
                    chars=gap_chars,
                )
            )
        gap = []
        gap_chars = 0

    for line_no, raw in enumerate(document.text.splitlines(), start=1):
        reason = _exclusion_reason(raw)
        if reason is None and _REFERENCE_HEADING_RE.match(raw.strip()):
            in_references = True
            reason = "references_section"
        elif reason is None and in_references:
            if _MARKDOWN_HEADING_RE.match(raw.strip()):
                in_references = False
            else:
                reason = "references_section"
        if reason is not None:
            excluded[reason] = excluded.get(reason, 0) + 1
            continue

        needle = _comparable(raw)
        if not needle:
            continue
        total_lines += 1
        char_count += len(needle)

        # A chapter or section heading is published as chunk metadata rather than
        # as chunk text, so it is checked against the heading paths and never
        # walks the body cursor: it is absent from the chunks by design, and
        # consuming it against them could match a phrase it shares with a later
        # article and drag the cursor past everything in between. The heading
        # path holds the words without the section number, so a line that carries
        # one is checked without it.
        if _is_published_heading(needle, headings):
            excluded["heading_metadata"] = excluded.get("heading_metadata", 0) + 1
            continue

        lost, cursor = _consume_line(needle, corpus, cursor)
        if lost == 0:
            covered_lines += 1
            flush_gap()
            continue
        missing_chars += lost
        gap_chars += lost
        gap.append((line_no, raw.strip(), lost))

    flush_gap()
    return ChunkCoverage(
        total_lines=total_lines,
        covered_lines=covered_lines,
        chunk_count=len(chunks),
        char_count=char_count,
        missing_chars=missing_chars,
        missing=missing,
        excluded=excluded,
    )


def prepare_chunks_for_publish(document: Document) -> list[Chunk]:
    """Chunk a document that passed the document gate, then gate the chunks.

    Chunking sits between the gates because damage can survive cleaning in a
    single clause while the document as a whole still reads healthy; grading
    each chunk keeps a clean majority from masking it. Coverage runs last
    because it is the only check that sees text the chunker never emitted at
    all — artifacts and structure cannot show a body that fell between them.
    """

    return prepared_from_document(document).require_publishable().chunks


@dataclass(frozen=True)
class PreparedSource:
    """One source parsed, cleaned, gated, chunked and coverage-checked.

    The two gate verdicts travel with the chunks, so the audit can report why a
    source would be refused while ingestion applies the very same verdicts
    through :meth:`require_publishable` instead of deriving its own.
    """

    document: Document
    chunks: list[Chunk]
    chunk_quality: QualityResult
    coverage: ChunkCoverage

    @property
    def blocks_publish(self) -> bool:
        return self.chunk_quality.status == "fail" or self.coverage.blocks_publish

    def require_publishable(self) -> PreparedSource:
        """Apply the two publication gates this preparation already evaluated."""

        if self.chunk_quality.status == "fail":
            raise ParseQualityError(self.document.doc_id, self.chunk_quality)
        if self.coverage.blocks_publish:
            raise ChunkCoverageError(self.document.doc_id, self.coverage)
        return self


def prepared_from_document(document: Document) -> PreparedSource:
    """Chunk a gated document and evaluate both publication gates."""

    chunks = chunk_document(document)
    quality = evaluate_chunks(chunk.text for chunk in chunks)
    coverage = check_chunk_coverage(document, chunks)
    return PreparedSource(document, chunks, quality, coverage)


def prepare_bound_document(document: Document, source: SourceRecord) -> PreparedSource:
    """Prepare a parsed document once its governed identity is known.

    The interactive CLI parses before it knows the identity, so it binds here
    rather than re-parsing; every other caller can use
    :func:`prepare_source_for_ingest` and skip this step.
    """

    return prepared_from_document(bind_source(document, source))


def prepare_source_for_ingest(
    source: SourceRecord, path: Path, *, parser: str = "auto"
) -> PreparedSource:
    """The formal entry: parse one governed source and prepare it for publishing.

    Parsing, cleaning, binding, the document gate, chunking and both chunk
    gates all happen behind this one call, so the audit and every ingest caller
    judge a source at exactly the boundary ingestion publishes it through.
    """

    return prepared_from_document(prepare_document_for_ingest(path, parser=parser, source=source))


__all__ = [
    "ChunkCoverage",
    "ChunkCoverageError",
    "CoverageGap",
    "ParseQualityError",
    "PreparedSource",
    "bind_source",
    "check_chunk_coverage",
    "prepare_bound_document",
    "prepare_chunks_for_publish",
    "prepare_document_for_ingest",
    "prepare_source_for_ingest",
    "prepared_from_document",
    "provisional_source",
    "record_quality",
]
