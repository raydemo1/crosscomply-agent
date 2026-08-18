"""Structure-aware chunking for Chinese laws and regulations."""

from __future__ import annotations

import re
from dataclasses import dataclass

from law_agent.data.citation_policy import can_cite_clause_chunk, citation_role_for_source
from law_agent.data.schemas import Chunk, Document

# PDF parsers such as Docling can serialize article headings as ``## 第一条``.
# Treat the Markdown prefix as presentation, not part of the legal identity.
ARTICLE_RE = re.compile(
    r"(?:#{1,6}\s+)?(?:\*\*)?(第[一二三四五六七八九十百千万零〇\d]+条)(?:\*\*)?"
)
ARTICLE_HEADING_RE = re.compile(
    r"^(?:#{1,6}\s+)?(?:\*\*)?(第[一二三四五六七八九十百千万零〇\d]+条)(?:\*\*)?"
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


def _strip_article_heading_markup(line: str) -> str:
    return ARTICLE_HEADING_RE.sub(r"\1", line, count=1)


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
    """Split legal text and carry chapter/section context into each article."""

    current_book: str | None = None
    current_part: str | None = None
    current_chapter: str | None = None
    current_section: str | None = None
    pending_article_no: str | None = None
    pending_lines: list[str] = []
    pending_path: list[str] = []
    articles: list[LawArticle] = []

    def flush() -> None:
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

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if BOOK_RE.match(line) and "条" not in line:
            flush()
            current_book = line
            current_part = None
            current_chapter = None
            current_section = None
            continue
        if PART_RE.match(line) and "条" not in line:
            flush()
            current_part = line
            current_chapter = None
            current_section = None
            continue
        if CHAPTER_RE.match(line) and "条" not in line:
            flush()
            current_chapter = line
            current_section = None
            continue
        if SECTION_RE.match(line) and "条" not in line:
            flush()
            current_section = line
            continue

        match = ARTICLE_HEADING_RE.match(line)
        if match:
            flush()
            pending_article_no = match.group(1)
            pending_path = [
                item
                for item in [
                    current_book,
                    current_part,
                    current_chapter,
                    current_section,
                    pending_article_no,
                ]
                if item
            ]
            pending_lines = [_strip_article_heading_markup(line)]
            continue

        if pending_article_no:
            pending_lines.append(line)

    flush()
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
    citation_role = citation_role_for_source(document.source_id)
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
                can_cite_clause=can_cite_clause_chunk(document.source_id, article_no),
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
