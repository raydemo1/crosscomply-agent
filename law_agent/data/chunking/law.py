"""Structure-aware chunking for Chinese laws and regulations."""

from __future__ import annotations

import re
from dataclasses import dataclass

from law_agent.data.citation_policy import can_cite_clause_chunk, citation_role_for_source
from law_agent.data.schemas import Chunk, Document

# PDF parsers such as Docling can serialize article headings as ``## 第一条``.
# Treat the Markdown prefix as presentation, not part of the legal identity.
ARTICLE_RE = re.compile(
    r"(?:#{1,6}\s+)?(?:\*\*)?(第[一二三四五六七八九十百千万零〇\d]+条)"
    r"(?!第?[一二三四五六七八九十百千万零〇\d]+[款项])(?:\*\*)?"
)
ARTICLE_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s+)?(?:\*\*)?(第[一二三四五六七八九十百千万零〇\d]+条)"
    r"(?!第?[一二三四五六七八九十百千万零〇\d]+[款项])(?:\*\*)?"
)
BOOK_RE = re.compile(r"^第[一二三四五六七八九十百千万零〇\d]+编")
PART_RE = re.compile(r"^第[一二三四五六七八九十百千万零〇\d]+篇")
CHAPTER_RE = re.compile(r"^第[一二三四五六七八九十百千万零〇\d]+章")
SECTION_RE = re.compile(r"^第[一二三四五六七八九十百千万零〇\d]+节")
ITEM_RE = re.compile(
    r"^(（[一二三四五六七八九十百千万零〇\d]+）|\([一二三四五六七八九十百千万零〇\d]+\))"
)
ARTICLE_HARD_LIMIT_CHARS = 650
MIN_PARAGRAPH_CHUNK_CHARS = 120
# Numerals a Chinese article number is written with, and the place units that
# multiply them ("二十三" = 23, "五百零九" = 509).
_NUMERAL_DIGITS = {
    "〇": 0, "零": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_NUMERAL_UNITS = {"十": 10, "百": 100, "千": 1000}
# Article numbers a body has to run through one after another before it counts
# as genuinely article-structured. One or two in a row is how prose quotes a
# statute ("属于……规定的第三条、第六条规定情形的"); an instrument that is
# actually built out of articles does not stop at two.
ORDERED_ARTICLE_RUN = 3


def _strip_article_heading_markup(line: str) -> str:
    return ARTICLE_HEADING_RE.sub(r"\1", line, count=1)


def article_ordinal(article_no: str) -> int | None:
    """Turn ``第十三条`` into 13; ``None`` for anything not a countable number.

    Public because the review Agent compares an article label it asked for
    ("第13条") against the label a chunk carries ("第十三条").
    """

    token = article_no.removeprefix("第").removesuffix("条").strip()
    if not token:
        return None
    if token.isdigit():
        return int(token)
    total = 0
    number = 0
    for char in token:
        if char in _NUMERAL_DIGITS:
            number = _NUMERAL_DIGITS[char]
        elif char in _NUMERAL_UNITS:
            total += (number or 1) * _NUMERAL_UNITS[char]
            number = 0
        else:
            return None
    return total + number


def has_ordered_articles(text: str) -> bool:
    """Whether the body carries a real, sequentially numbered article structure.

    Routing used to infer article structure from "three article markers
    anywhere in the text". A negative list satisfies that by quoting
    ``属于《促进和规范数据跨境流动规定》第三条、第六条规定情形的`` inside a
    table cell, because the PDF text layer wraps the sentence so the line starts
    with ``第六条``. Requiring the marker to start a line *and* the numbers to
    advance one by one means a quotation inside prose can never stand in for an
    article structure.

    Prose between two articles does not end the chain: a real instrument writes
    its articles over as many lines as it needs, so only the numbering itself
    has to be consecutive.
    """

    run = 0
    previous: int | None = None
    for raw_line in text.splitlines():
        match = ARTICLE_HEADING_RE.match(raw_line.strip())
        if match is None:
            continue
        ordinal = article_ordinal(match.group(1))
        if ordinal is None:
            previous = None
            run = 0
            continue
        run = run + 1 if previous is not None and ordinal == previous + 1 else 1
        previous = ordinal
        if run >= ORDERED_ARTICLE_RUN:
            return True
    return False


@dataclass(frozen=True)
class LawArticle:
    article_no: str
    text: str
    heading_path: list[str]


@dataclass(frozen=True)
class LawUnit:
    article_no: str
    text: str
    heading_path: list[str]
    paragraph_no: str | None = None
    item_no: str | None = None


def split_law_articles(text: str) -> list[tuple[str, str]]:
    """Split Chinese legal text by article markers while preserving article numbers."""

    matches = list(ARTICLE_RE.finditer(text))
    if not matches:
        stripped = text.strip()
        return [("", stripped)] if stripped else []

    articles: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.start()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        article_no = match.group(1)
        article_text = text[start:end].strip()
        article_text = _strip_article_heading_markup(article_text)
        if article_text:
            articles.append((article_no, article_text))
    return articles


def split_law_article_sections(text: str) -> list[LawArticle]:
    """Split legal text and carry chapter/section context into each article.

    Text sitting outside any article — a substantive preamble before the first
    one, or a passage a chapter heading interrupts — comes back as its own
    section with an empty ``article_no``. Only a bare chapter heading stays
    metadata; dropping the prose around it lost real body text.
    """

    current_book: str | None = None
    current_part: str | None = None
    current_chapter: str | None = None
    current_section: str | None = None
    pending_article_no: str | None = None
    pending_lines: list[str] = []
    pending_path: list[str] = []
    loose_lines: list[str] = []
    loose_path: list[str] = []
    articles: list[LawArticle] = []

    def context() -> list[str]:
        return [
            item
            for item in [current_book, current_part, current_chapter, current_section]
            if item
        ]

    def flush_article() -> None:
        nonlocal pending_article_no, pending_lines, pending_path
        if pending_article_no and pending_lines:
            articles.append(
                LawArticle(
                    article_no=pending_article_no,
                    text="\n".join(pending_lines).strip(),
                    heading_path=pending_path,
                )
            )
        pending_article_no = None
        pending_lines = []
        pending_path = []

    def flush_loose() -> None:
        nonlocal loose_lines, loose_path
        if loose_lines:
            articles.append(
                LawArticle(
                    article_no="",
                    text="\n".join(loose_lines).strip(),
                    heading_path=loose_path,
                )
            )
        loose_lines = []
        loose_path = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if BOOK_RE.match(line) and "条" not in line:
            flush_article()
            flush_loose()
            current_book = line
            current_part = None
            current_chapter = None
            current_section = None
            continue
        if PART_RE.match(line) and "条" not in line:
            flush_article()
            flush_loose()
            current_part = line
            current_chapter = None
            current_section = None
            continue
        if CHAPTER_RE.match(line) and "条" not in line:
            flush_article()
            flush_loose()
            current_chapter = line
            current_section = None
            continue
        if SECTION_RE.match(line) and "条" not in line:
            flush_article()
            flush_loose()
            current_section = line
            continue

        match = ARTICLE_HEADING_RE.match(line)
        if match:
            flush_article()
            flush_loose()
            pending_article_no = match.group(1)
            pending_path = [*context(), pending_article_no]
            pending_lines = [_strip_article_heading_markup(line)]
            continue

        if pending_article_no:
            pending_lines.append(line)
            continue

        if not loose_lines:
            loose_path = context()
        loose_lines.append(line)

    flush_article()
    flush_loose()
    return articles


def split_law_units(article: LawArticle) -> list[LawUnit]:
    """Use article-first chunks; split to paragraphs only for oversized articles."""

    lines = [line.strip() for line in article.text.splitlines() if line.strip()]
    if len(article.text) <= ARTICLE_HARD_LIMIT_CHARS or len(lines) <= 1:
        return [
            LawUnit(
                article_no=article.article_no,
                text=article.text,
                heading_path=article.heading_path,
            )
        ]

    units: list[LawUnit] = []
    paragraph_index = 0
    current_lines: list[str] = []
    current_paragraph_no: str | None = None

    def flush() -> None:
        nonlocal current_lines, current_paragraph_no
        if not current_lines or current_paragraph_no is None:
            return
        paragraph_text = "\n".join(current_lines)
        units.append(
            LawUnit(
                article_no=article.article_no,
                text=paragraph_text,
                heading_path=[*article.heading_path, current_paragraph_no],
                paragraph_no=current_paragraph_no,
            )
        )
        current_lines = []
        current_paragraph_no = None

    for line in lines:
        if ITEM_RE.match(line) and current_lines:
            current_lines.append(line)
            continue

        flush()
        paragraph_index += 1
        current_paragraph_no = f"第{paragraph_index}款"
        current_lines = [line]

    flush()

    if len(units) <= 1:
        return [
            LawUnit(
                article_no=article.article_no,
                text=article.text,
                heading_path=article.heading_path,
            )
        ]

    merged: list[LawUnit] = []
    for unit in units:
        if (
            merged
            and len(unit.text) < MIN_PARAGRAPH_CHUNK_CHARS
            and len(merged[-1].text) + len(unit.text) <= ARTICLE_HARD_LIMIT_CHARS
        ):
            previous = merged[-1]
            merged[-1] = LawUnit(
                article_no=previous.article_no,
                text=f"{previous.text}\n{unit.text}",
                heading_path=previous.heading_path,
                paragraph_no=previous.paragraph_no,
            )
        else:
            merged.append(unit)

    return merged


def chunk_law_document(document: Document) -> list[Chunk]:
    """Create retrieval chunks for a law-like document."""

    article_sections = split_law_article_sections(document.text)
    if not article_sections:
        article_sections = [
            LawArticle(article_no=article_no, text=article_text, heading_path=[article_no])
            for article_no, article_text in split_law_articles(document.text)
        ]
    units = [unit for article in article_sections for unit in split_law_units(article)]
    chunks: list[Chunk] = []
    citation_role = citation_role_for_source(document)
    for index, unit in enumerate(units):
        chunk_id = f"{document.doc_id}:{index:04d}"
        heading_path = [document.title, *unit.heading_path]
        article_no = unit.article_no or None
        citation_parts = [document.title, unit.article_no, unit.paragraph_no, unit.item_no]
        citation_label = " ".join(part for part in citation_parts if part)
        chunks.append(
            Chunk(
                chunk_id=chunk_id,
                doc_id=document.doc_id,
                source_id=document.source_id,
                library_kind=document.library_kind,
                title=document.title,
                text=unit.text,
                chunk_index=index,
                doc_type=document.doc_type,
                heading_path=heading_path,
                article_no=article_no,
                paragraph_no=unit.paragraph_no,
                item_no=unit.item_no,
                citation_label=citation_label,
                citation_role=citation_role,
                can_cite_clause=can_cite_clause_chunk(document, article_no),
                prev_chunk_id=f"{document.doc_id}:{index - 1:04d}" if index > 0 else None,
                next_chunk_id=(
                    f"{document.doc_id}:{index + 1:04d}" if index + 1 < len(units) else None
                ),
                authority=document.authority,
                law_status=document.law_status,
                publish_date=document.publish_date,
                effective_date=document.effective_date,
                source_url=document.source_url,
                applicable_region=document.applicable_region,
                issuing_body=document.issuing_body,
                owning_department=document.owning_department,
                internal_status=document.internal_status,
                legal_domain=document.legal_domain,
                applicable_subjects=document.applicable_subjects,
                case_no=document.case_no,
                court=document.court,
                trial_instance=document.trial_instance,
                contract_parties=document.contract_parties,
                clause_type=document.clause_type,
                topic_tags=document.topic_tags,
                char_count=len(unit.text),
            )
        )
    return chunks
