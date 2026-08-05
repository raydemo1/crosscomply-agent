"""Deterministic text cleaning helpers shared by data sources."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CONTROL_CHARS_RE = re.compile(r"[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]")
TRAILING_SPACE_RE = re.compile(r"[ \t]+\n")
BLANK_LINES_RE = re.compile(r"\n{3,}")
CONTENTS_TITLE_RE = re.compile(r"^(contents|目录|目次|tableofcontents)$", re.IGNORECASE)
STRUCTURAL_HEADING_RE = re.compile(r"^(?:#{1,6}\s+)?第[一二三四五六七八九十百千万零〇\d]+[章节编]")
DOT_LEADER_TOC_RE = re.compile(r"^\s*.+?[\.·…．]{4,}\s*[IVXLCDM\d]+\s*$", re.IGNORECASE)
# Broad leader detection: 4+ consecutive dots/leaders anywhere in the line.
DOT_LEADER_BROAD_RE = re.compile(r"[\.·…．]{4,}")
# Table-style TOC line: starts with | and contains dot leaders (PDF→markdown tables).
TABLE_TOC_LINE_RE = re.compile(r"^\|.*[\.·…．]{4,}")
# Docling can emit a standard's whole contents page as a Markdown table, even
# when its cells no longer include page-number dot leaders.
MARKDOWN_TABLE_ROW_RE = re.compile(r"^\|.*\|\s*$")
# Front-matter body markers that signal end of a TOC block.
FRONT_MATTER_BODY_RE = re.compile(r"^(?:#{1,6}\s+)?(前\s*言|引\s*言)\b")
# Isolated clause-number line from standard PDFs: bare "3.2", "5", "5.4.1".
ISOLATED_NUMBER_LINE_RE = re.compile(r"^\d+(?:\.\d+){0,4}$")
# PDF character-spacing artifact: "D a t a s e c u r i t y" (single letters spaced out).
# Captures the preceding boundary char (group 1) + the spaced letter run (group 2).
SPACED_LATIN_RE = re.compile(r"(^|[^\w])([a-zA-Z](?:\s[a-zA-Z]){4,})")
WEB_BOILERPLATE_PATTERNS = [
    re.compile(pattern)
    for pattern in [
        r"^Title:\s+",
        r"^URL Source:\s+",
        r"^Markdown Content:\s*$",
        r"^\d{4}年\d{2}月\d{2}日 星期[一二三四五六日天]$",
        r"^\[设为首页\].*\[加入收藏\].*",
        r"^\[搜索\]\(",
        r"^\*?\s*!\[Image\b",
        r"^\[!\[Image\b",
        r"^!\[Image\b",
        r"^\*?\s*### \[?(首 页|首页|时政要闻|网信政务|互动服务|热点专题)",
        r"^\*?\s*\[(首页|时政要闻|网信政务|互动服务|热点专题)\]",
        r"^当前位置：",
        r"^\[\]\(",
        r"^\[【打印】\]",
        r"^关闭$",
        r"^中央网络安全和信息化委员会办公室",
        r"^中华人民共和国国家互联网信息办公室 © 版权所有",
        r"^承办：",
        r"^技术支持",
        r"^京ICP备",
        r"^\[京公网安备",
        r"^Produced By CMS",
        r"^分享到微信",
        r"^打开微信",
        r"^使用“扫一扫”",
        r"^\*\s+######\s*(学习强国|微信|返回顶部)",
        r"^_◆_◆",
    ]
]
IMAGE_PLACEHOLDER_RE = re.compile(r"^<!--\s*image\s*-->$", re.IGNORECASE)
MARKDOWN_TABLE_DIVIDER_RE = re.compile(r"^\|[\s:|-]+\|?$")
PDF_PAGE_COUNT_RE = re.compile(r"^Number of Pages:\s*\d+\s*$", re.IGNORECASE)
SOURCE_AVAILABILITY_RE = re.compile(r"^本文档可从以下网址获得[:：]?$")
STANDALONE_SOURCE_URL_RE = re.compile(
    r"^(?:\[)?(?:https?://)?(?:www\.)?(?:tc260\.org\.cn|cac\.gov\.cn|flk\.npc\.gov\.cn)[^\s\]]*(?:\]\([^)]+\))?$",
    re.IGNORECASE,
)
MARKDOWN_SOURCE_LINK_RE = re.compile(
    r"^\[[^\]]*(?:tc260\.org\.cn|cac\.gov\.cn|flk\.npc\.gov\.cn)[^\]]*\]\([^)]+\)$",
    re.IGNORECASE,
)
STANDALONE_YEAR_MONTH_RE = re.compile(r"^20\d{2}\s*年\s*\d{1,2}\s*月$")
PDF_COVER_FRAGMENT_RE = re.compile(r"^(?:TECHNICAL COMMITTEE|Cyber|维化技术|术委员会|[会委])$")


@dataclass(frozen=True)
class CleanResult:
    """Text after cleaning plus rule hit counters."""

    text: str
    rule_hits: dict[str, int] = field(default_factory=dict)


def _apply_counted_sub(pattern: re.Pattern[str], replacement: str, text: str) -> tuple[str, int]:
    return pattern.subn(replacement, text)


def _compact_heading(value: str) -> str:
    """Strip markdown heading prefix and all whitespace for title matching."""
    return re.sub(r"[\s\u3000#]+", "", value.strip())


def _is_toc_line(stripped: str) -> bool:
    """True if a stripped line looks like a table-of-contents entry or table divider."""
    if not stripped:
        return False
    if DOT_LEADER_BROAD_RE.search(stripped):
        return True
    if TABLE_TOC_LINE_RE.match(stripped):
        return True
    # Bare page-number line (roman or arabic) inside a TOC block.
    if re.fullmatch(r"[IVXLCDM\d]+", stripped, re.IGNORECASE):
        return True
    # Markdown table divider line: |---|---| or | ---- |
    return bool(re.fullmatch(r"\|[\s:|-]+", stripped))


def _is_body_start(stripped: str) -> bool:
    """True if a stripped line signals the end of a TOC block (real body).."""
    if not stripped:
        return False
    if FRONT_MATTER_BODY_RE.match(stripped):
        return True
    if STRUCTURAL_HEADING_RE.match(stripped):
        return True
    # Standard body start: "1 范围" / "1范围"
    if re.match(r"^1\s*(范围|适用范围)", stripped):
        return True
    # Article marker
    return bool(re.match(r"^(?:#{1,6}\s+)?第[一二三四五六七八九十百千万零〇\d]+条", stripped))


def _remove_contents_table(lines: list[str]) -> tuple[list[str], int]:
    """Remove table-of-contents blocks for both laws and standards.

    Law-style TOC: 目录 heading → chapter list → chapter list repeats.
    Standard-style TOC: 目 次 heading → dot-leader/table lines → 前言/引言.
    """

    cleaned: list[str] = []
    removed = 0
    index = 0
    while index < len(lines):
        compact = _compact_heading(lines[index])
        if not CONTENTS_TITLE_RE.match(compact):
            cleaned.append(lines[index])
            index += 1
            continue

        # --- Strategy A: law-style repeated chapter headings ---
        cursor = index + 1
        catalog_headings: list[str] = []
        while cursor < len(lines):
            candidate = lines[cursor].strip()
            if not candidate:
                cursor += 1
                continue
            if catalog_headings and candidate == catalog_headings[0]:
                break
            if STRUCTURAL_HEADING_RE.match(candidate) and "条" not in candidate:
                catalog_headings.append(candidate)
                cursor += 1
                continue
            break

        if catalog_headings and cursor < len(lines) and lines[cursor].strip() == catalog_headings[0]:
            removed += cursor - index
            index = cursor
            continue

        # --- Strategy B: a PDF contents page serialized as a Markdown table ---
        # This is deliberately limited to rows immediately after a contents
        # heading, so ordinary tables in the document body are never removed.
        cursor = index + 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        if cursor < len(lines) and MARKDOWN_TABLE_ROW_RE.match(lines[cursor].strip()):
            table_end = cursor
            while table_end < len(lines):
                candidate = lines[table_end].strip()
                if not candidate:
                    table_end += 1
                    continue
                if MARKDOWN_TABLE_ROW_RE.match(candidate):
                    table_end += 1
                    continue
                break
            removed += table_end - index
            index = table_end
            continue

        # --- Strategy C: standard-style TOC block (dot leaders / tables) ---
        cursor = index + 1
        block_end = index + 1
        saw_toc_line = False
        max_scan = min(len(lines), index + 60)
        while cursor < max_scan:
            candidate = lines[cursor].strip()
            if not candidate:
                cursor += 1
                continue
            if _is_body_start(candidate):
                block_end = cursor
                break
            if _is_toc_line(candidate):
                saw_toc_line = True
                cursor += 1
                continue
            # Non-TOC, non-body line: stop if we already saw TOC lines.
            if saw_toc_line:
                block_end = cursor
                break
            # Otherwise this TOC heading has no TOC body; keep it.
            block_end = index + 1
            break

        if saw_toc_line and block_end > index + 1:
            removed += block_end - index
            index = block_end
            continue

        cleaned.append(lines[index])
        index += 1

    return cleaned, removed


def _remove_mechanical_lines(lines: list[str]) -> tuple[list[str], dict[str, int]]:
    cleaned: list[str] = []
    hits = {
        "dot_leader_toc_lines": 0,
        "web_boilerplate_lines": 0,
        "image_placeholder_lines": 0,
        "table_divider_lines": 0,
        "pdf_page_count_lines": 0,
        "source_availability_lines": 0,
        "standalone_source_url_lines": 0,
        "standalone_year_month_lines": 0,
        "pdf_cover_fragment_lines": 0,
    }

    for line in lines:
        stripped = line.strip()
        stripped_plain = stripped.strip("[]()")
        if IMAGE_PLACEHOLDER_RE.match(stripped):
            hits["image_placeholder_lines"] += 1
            continue
        if MARKDOWN_TABLE_DIVIDER_RE.match(stripped):
            hits["table_divider_lines"] += 1
            continue
        if PDF_PAGE_COUNT_RE.match(stripped):
            hits["pdf_page_count_lines"] += 1
            continue
        if SOURCE_AVAILABILITY_RE.match(stripped):
            hits["source_availability_lines"] += 1
            continue
        if STANDALONE_SOURCE_URL_RE.match(stripped_plain) or MARKDOWN_SOURCE_LINK_RE.match(stripped):
            hits["standalone_source_url_lines"] += 1
            continue
        if STANDALONE_YEAR_MONTH_RE.match(stripped):
            hits["standalone_year_month_lines"] += 1
            continue
        if PDF_COVER_FRAGMENT_RE.match(stripped):
            hits["pdf_cover_fragment_lines"] += 1
            continue
        if DOT_LEADER_TOC_RE.match(stripped):
            hits["dot_leader_toc_lines"] += 1
            continue
        if any(pattern.search(stripped) for pattern in WEB_BOILERPLATE_PATTERNS):
            hits["web_boilerplate_lines"] += 1
            continue
        cleaned.append(line)

    return cleaned, {key: value for key, value in hits.items() if value}


def _fix_spaced_latin(lines: list[str]) -> tuple[list[str], int]:
    """Collapse PDF character-spacing artifacts like "D a t a s e c u r i t y"."""

    fixed_count = 0
    result: list[str] = []

    def _collapse(match: re.Match[str]) -> str:
        nonlocal fixed_count
        fixed_count += 1
        # group(1) = boundary char ("", or non-word char); group(2) = spaced run.
        boundary = match.group(1)
        collapsed = re.sub(r"\s", "", match.group(2))
        return f"{boundary}{collapsed}"

    for line in lines:
        new_line = SPACED_LATIN_RE.sub(_collapse, line)
        result.append(new_line)

    return result, fixed_count


def _merge_isolated_number_lines(lines: list[str]) -> tuple[list[str], int]:
    """Merge bare clause-number lines (e.g. "3.2") into the following heading line.

    Standard PDFs frequently emit the clause number and its title on separate
    lines.  Keeping them isolated produces single-character chunks downstream.
    """

    merged_count = 0
    result: list[str] = []
    skip_next = False

    for index, line in enumerate(lines):
        if skip_next:
            skip_next = False
            continue
        stripped = line.strip()
        if ISOLATED_NUMBER_LINE_RE.match(stripped):
            # Look ahead for a non-empty line that is NOT another isolated number.
            lookahead = index + 1
            while lookahead < len(lines) and not lines[lookahead].strip():
                lookahead += 1
            if lookahead < len(lines):
                next_stripped = lines[lookahead].strip()
                if not ISOLATED_NUMBER_LINE_RE.match(next_stripped):
                    # Strip markdown heading prefix from the following line so the
                    # merged result reads "3.2 个人信息" not "3.2 ## 个人信息".
                    heading_match = re.match(r"^(#{1,6})\s+(.+)$", next_stripped)
                    next_title = heading_match.group(2).strip() if heading_match else next_stripped
                    result.append(f"{stripped} {next_title}")
                    skip_next = True
                    merged_count += 1
                    continue
        result.append(line)

    return result, merged_count


def clean_text(text: str, *, title: str | None = None) -> CleanResult:
    """Clean mechanical noise without rewriting legal text."""

    hits: dict[str, int] = {}
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    text, count = _apply_counted_sub(CONTROL_CHARS_RE, "", text)
    hits["control_chars"] = count

    text, count = _apply_counted_sub(TRAILING_SPACE_RE, "\n", text)
    hits["trailing_space"] = count

    if title:
        lines = text.split("\n")
        cleaned_lines: list[str] = []
        seen_title = False
        duplicate_title_count = 0
        for line in lines:
            if line.strip() == title.strip():
                if seen_title:
                    duplicate_title_count += 1
                    continue
                seen_title = True
            cleaned_lines.append(line)
        text = "\n".join(cleaned_lines)
        hits["duplicate_title"] = duplicate_title_count

    lines, count = _remove_contents_table(text.split("\n"))
    lines, line_hits = _remove_mechanical_lines(lines)
    hits.update(line_hits)
    text = "\n".join(lines)
    hits["contents_table_lines"] = count

    # Fix PDF character-spacing artifacts ("D a t a" → "Data").
    lines = text.split("\n")
    lines, count = _fix_spaced_latin(lines)
    hits["spaced_latin"] = count
    text = "\n".join(lines)

    # Merge isolated clause-number lines into following heading lines.
    lines = text.split("\n")
    lines, count = _merge_isolated_number_lines(lines)
    hits["merged_isolated_numbers"] = count
    text = "\n".join(lines)

    text, count = _apply_counted_sub(BLANK_LINES_RE, "\n\n", text.strip())
    hits["blank_lines"] = count

    return CleanResult(text=text + "\n", rule_hits={key: value for key, value in hits.items() if value})
