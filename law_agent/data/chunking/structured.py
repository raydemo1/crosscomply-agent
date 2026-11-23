"""Structure-aware chunking for non-article legal materials."""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import unescape

from law_agent.data.citation_policy import citation_role_for_source
from law_agent.data.schemas import Chunk, Document

GENERIC_HARD_LIMIT_CHARS = 650
GENERIC_SOFT_LIMIT_CHARS = 480
MIN_GENERIC_CHUNK_CHARS = 120

MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
NUMERIC_HEADING_RE = re.compile(r"^(\d+(?:\.\d+){0,4})[\.、]?\s+(.{1,80})$")
APPENDIX_HEADING_RE = re.compile(r"^(?:#{1,6}\s+)?附\s*录\s*([A-Z])(?:[\s（(]|$)")
APPENDIX_SUBHEADING_RE = re.compile(r"^([A-Z])\.(\d+(?:\.\d+){0,4})[\.、]?\s+(.{1,80})$")
# A question line in a Q&A: "问：…", "问3：…", "Q: …", or a numbered question "2.…？".
QUESTION_HEADING_RE = re.compile(r"^(?:(?:问|Q|问题)\s*\d{0,3}\s*[:：].+|\d{1,3}\s*[\.．、]\s*\S.{2,200}[？?])$")
TABLE_RE = re.compile(r"<table\b.*?</table>", re.IGNORECASE | re.DOTALL)
ROW_RE = re.compile(r"<tr\b.*?</tr>", re.IGNORECASE | re.DOTALL)
CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
TAG_RE = re.compile(r"<[^>]+>")
DECORATIVE_TABLE_LINE_RE = re.compile(r"^\|[-:| \t]+\|?$")
# Everything a table shard can consist of without holding a single character of
# text: layout whitespace, pipes, rules and the colons of an alignment row.
_LAYOUT_ONLY_RE = re.compile(r"[\s|:\-]+")
# Markdown table: a header row, a separator row (|---|---|), and 1+ data rows.
# Each row starts and ends with `|`. The separator row only contains `-`, `:`, `|`, spaces.
MARKDOWN_TABLE_RE = re.compile(
    r"(?:^[ \t]*\|[^\n]*\|[ \t]*\n)"  # header row
    r"(?:[ \t]*\|[-:| \t]+\|[ \t]*\n)"  # separator row
    r"(?:[ \t]*\|[^\n]*\|[ \t]*\n?)+",  # 1+ data rows
    re.MULTILINE,
)
MARKDOWN_TABLE_ROW_RE = re.compile(r"^[ \t]*\|[^\n]*\|[ \t]*$", re.MULTILINE)
MARKDOWN_TABLE_SEP_RE = re.compile(r"^[ \t]*\|[-:| \t]+\|[ \t]*$", re.MULTILINE)


@dataclass(frozen=True)
class StructuredUnit:
    text: str
    heading_path: list[str]
    citation_label: str | None = None


def chunk_structured_document(document: Document) -> list[Chunk]:
    """Create retrieval chunks for guides, Q&A, standards, and table-heavy lists."""

    if document.doc_type == "faq":
        units = split_faq_units(document.text)
    elif _looks_table_heavy(document.text):
        units = split_table_aware_units(document.text)
    else:
        units = split_heading_units(document.text)

    if not units:
        units = split_plain_units(document.text, [])

    return _chunks_from_units(document, units)


def split_faq_units(text: str) -> list[StructuredUnit]:
    units: list[StructuredUnit] = []
    current_heading: str | None = None
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_heading, current_lines
        if current_lines:
            heading = current_heading or "问答"
            units.extend(_split_long_text("\n".join(current_lines), [heading], heading))
        current_heading = None
        current_lines = []

    for raw_line in _meaningful_lines(text):
        line = _clean_inline_markup(_strip_markdown_heading(raw_line))
        if QUESTION_HEADING_RE.match(line):
            flush()
            current_heading = line[:120]
            current_lines = [line]
            continue
        # Text before the first question is the Q&A's own lead-in — what the
        # session was about and who answered. It belongs in a chunk like any
        # other body text, so it accumulates under the "问答" heading instead of
        # being discarded until the first question appears.
        current_lines.append(line)

    flush()
    if units:
        return units
    return split_heading_units(text)


def split_table_aware_units(text: str) -> list[StructuredUnit]:
    """Split text that contains tables (HTML or markdown) into chunks.

    Markdown tables (produced by docling/TableFormerV2) are converted to HTML
    so the existing HTML table chunking logic can handle them. This preserves
    row-level structure instead of letting tables be split mid-row by the
    generic 480-char splitter.
    """
    # Convert markdown tables to HTML so the existing table chunking logic
    # (which expects <table><tr><td>) handles them uniformly.
    text = _markdown_tables_to_html(text)

    units: list[StructuredUnit] = []
    cursor = 0
    heading_path: list[str] = []

    for table_index, match in enumerate(TABLE_RE.finditer(text), start=1):
        before = text[cursor : match.start()]
        before_units = split_heading_units(before)
        if before_units:
            units.extend(before_units)
            heading_path = before_units[-1].heading_path
        else:
            heading_path = _last_context_heading(before, heading_path)

        units.extend(split_table_units(match.group(0), heading_path, f"表格{table_index}"))
        cursor = match.end()

    tail_units = split_heading_units(text[cursor:])
    units.extend(tail_units)
    return _merge_tiny_units(units)


def _markdown_tables_to_html(text: str) -> str:
    """Convert markdown tables to HTML <table> blocks.

    A markdown table has a header row, a separator row (|---|---|), and 1+
    data rows. Each cell is the text between `|` delimiters. The separator
    row is dropped. Empty cells are preserved as `<td></td>` so column
    alignment is retained (important for "数据类别" columns that are sometimes
    blank in the source documents).
    """

    def replacer(match: re.Match[str]) -> str:
        table_text = match.group(0)
        rows: list[list[str]] = []
        for line in table_text.splitlines():
            stripped = line.strip()
            if not stripped or MARKDOWN_TABLE_SEP_RE.match(stripped):
                continue
            # Split on `|`, drop the first/last empty parts (from leading/trailing `|`)
            parts = stripped.split("|")
            if len(parts) >= 2 and parts[0].strip() == "" and parts[-1].strip() == "":
                parts = parts[1:-1]
            cells = [_strip_tags(p.strip()) for p in parts]
            rows.append(cells)
        if len(rows) < 2:
            return table_text  # not a real table, leave as-is
        html_rows = []
        for row in rows:
            tag = "th" if row is rows[0] else "td"
            html_rows.append("<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in row) + "</tr>")
        return "<table>" + "".join(html_rows) + "</table>"

    return MARKDOWN_TABLE_RE.sub(replacer, text)


TABLE_SECTION_RE = re.compile(r"^(?:([一二三四五六七八九十]+、)|(（[一二三四五六七八九十]+）)|(\d+[\.、．](?!\d)))")
TABLE_NOTE_RE = re.compile(r"^(?:注|备注|说明)\s*\d*\s*[:：]")


def _table_rows(table_html: str) -> list[list[str]]:
    rows = [[_strip_tags(cell.group(1)) for cell in CELL_RE.finditer(row.group(0))]
            for row in ROW_RE.finditer(table_html)]
    return [row for row in rows if any(row)]


def _is_title_row(row: list[str], width: int) -> bool:
    """A row the table spans across: one value, in a table that has more columns."""

    # A row holding only a number in its first cell ("1" in an empty form) is a data row, not a title.
    return (width > 1 and len(row[0]) <= 120 and not any(row[1:])
            and bool(re.search(r"[\u4e00-\u9fff]", row[0])))


def _is_column_header(row: list[str]) -> bool:
    cells = [cell for cell in row if cell]
    return len(cells) >= 2 and all(
        len(cell) <= 20 and not re.search(r"[。；]", cell) and not re.match(r"^\d", cell) for cell in cells
    )


def _title_level(value: str) -> int:
    match = TABLE_SECTION_RE.match(value)
    if not match:
        return 1
    return 1 if match.group(1) else 2 if match.group(2) else 3


def split_table_units(table_html: str, heading_path: list[str], table_label: str) -> list[StructuredUnit]:
    """One unit per data row, each stating where in the table it sits.

    A negative list is one long table whose meaning depends on rows that span
    it: the industry ("一、医药行业"), the list a row belongs to ("（一）需要通过
    数据出境安全评估的数据清单" vs "（二）……标准合同……"), and the column
    header. Those rows open sections of the table; every data row is published
    with its section path and column header so a retrieved row still says which
    procedure it triggers. A row too short to stand alone joins the row before
    it in the same section.
    """

    rows = _table_rows(table_html)
    if not rows:
        return split_plain_units(_strip_tags(table_html), heading_path)
    width = max(len(row) for row in rows)
    sections: list[str] = []
    header: list[str] | None = None
    units: list[StructuredUnit] = []
    last_path: list[str] | None = None
    header_used = False
    last_header: list[str] | None = None
    title_pending = False  # a title row that no row has been published under yet

    def path() -> list[str]:
        return [*heading_path, *sections] if sections else [*heading_path, table_label]

    def emit(body: str, *, joinable: bool) -> None:
        nonlocal last_path
        current = path()
        # The spanning rows are written into the text, not only the metadata, so the words that decide a
        # row's meaning ("（一）需要通过数据出境安全评估的数据清单") are searchable and shown with it.
        # They are joined as table cells are, which keeps them in the source's reading order.
        context = " | ".join(sections)
        if (joinable and units and last_path == current and len(body) < MIN_GENERIC_CHUNK_CHARS
                and len(units[-1].text) + len(body) + 1 <= GENERIC_SOFT_LIMIT_CHARS):
            previous = units[-1]
            units[-1] = StructuredUnit(text=f"{previous.text}\n{body}", heading_path=previous.heading_path,
                                       citation_label=previous.citation_label)
            return
        columns = header or last_header  # a section without its own header row reads under the table's last one
        lead = "\n".join(part for part in (context, " | ".join(columns) if columns and joinable else "") if part)

        def with_lead(text: str) -> str:
            return f"{lead}\n{text}" if lead else text

        if len(body) + len(lead) + 1 > GENERIC_HARD_LIMIT_CHARS:
            pieces = _split_long_text(body, current, " ".join(current))
            units.extend(StructuredUnit(text=with_lead(piece.text), heading_path=current,
                                        citation_label=" ".join(current)) for piece in pieces)
        else:
            units.append(StructuredUnit(text=with_lead(body), heading_path=current,
                                        citation_label=" ".join(current)))
        last_path = current

    for row in rows:
        if width > 1 and TABLE_NOTE_RE.match(row[0]) and not any(row[1:]):
            # A note spanning the table ("注：……") is published as written, of any length, without a header.
            emit(row[0], joinable=False)
            title_pending = False
            continue
        if _is_title_row(row, width):
            value = row[0]
            if header is not None and not header_used:
                lone, header = " | ".join(header), None
                emit(lone, joinable=False)
                title_pending = False
            level = _title_level(value)
            if title_pending and level <= len(sections):
                emit(sections[-1], joinable=False)  # a section with no rows still says it exists
            sections = [*sections[: level - 1], value]
            header = None
            last_path = None
            title_pending = True
            continue
        if header is not None and row == header:
            # A header repeated on every page of the original: the next row opens a unit of its own,
            # which then carries the header, keeping the source's order.
            last_path = None
            continue
        if header is None and _is_column_header(row):
            # Only the first row of a section can be its column header; later short rows are data.
            header = last_header = row
            header_used = False
            last_path = None
            continue
        emit(" | ".join(row), joinable=True)
        header_used = True
        title_pending = False
    if header is not None and not header_used:
        header, lone = None, " | ".join(header)
        emit(lone, joinable=False)
    elif title_pending:
        emit(sections[-1], joinable=False)
    return units


# A heading's own section number states its depth: "4.1" is a subsection of "4"
# whatever Markdown level the parser flattened it to.
SECTION_NUMBER_RE = re.compile(r"^([A-Z]?\.?\d+(?:\.\d+){0,4})[\.、]?(?:\s|$)")


def _heading_level(value: str, markdown_level: int) -> int:
    """Depth of a heading, taken from its section number when it has one.

    A parser that renders an outline flat emits "## 4 评估原理" beside
    "## 4.1 概述". Kept at one level, the second heading truncates the first off
    every heading path, so a parent heading whose section holds no direct body
    text vanishes from the document. The number is the outline the Markdown
    level failed to carry, so it decides when present.
    """

    number = SECTION_NUMBER_RE.match(value)
    if number:
        return min(number.group(1).count(".") + 1, 6)
    return markdown_level


def split_heading_units(text: str) -> list[StructuredUnit]:
    units: list[StructuredUnit] = []
    heading_stack: list[str] = []
    current_heading: str | None = None
    current_path: list[str] = []
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_heading, current_path, current_lines
        # A section that holds nothing but its heading is still document text:
        # an outline has parents and leaves that carry no direct body. Dropping
        # that line here loses it twice over, because the next sibling truncates
        # the heading stack it was sitting on, so it survives neither as chunk
        # text nor in any later heading path. It is published as its own unit.
        if current_lines:
            citation = " ".join(current_path) if current_path else None
            units.extend(_split_long_text("\n".join(current_lines), current_path, citation))
        current_heading = None
        current_path = []
        current_lines = []

    for raw_line in _meaningful_lines(text):
        if DECORATIVE_TABLE_LINE_RE.match(raw_line.strip()):
            continue
        heading = _heading_from_line(raw_line)
        if heading is not None:
            flush()
            level, value = heading
            heading_stack = _push_heading(heading_stack, level, value)
            current_heading = value
            current_path = [item for item in heading_stack if item]
            current_lines = [value]
            continue
        if current_heading is None:
            current_path = []
        current_lines.append(_strip_markdown_heading(raw_line))

    flush()
    return _merge_tiny_units(units)


def _push_heading(stack: list[str], level: int, value: str) -> list[str]:
    if stack and re.match(r"^[一二三四五六七八九十]+、", stack[0]) and SECTION_NUMBER_RE.match(value):
        level = min(level + 2, 6)
    updated = stack[:level - 1]
    while len(updated) < level - 1:
        updated.append("")
    return [*updated, value]


def split_plain_units(text: str, heading_path: list[str]) -> list[StructuredUnit]:
    return _split_long_text("\n".join(_meaningful_lines(_strip_tags(text))), heading_path, None)


def _is_layout_shard(text: str) -> bool:
    """True when a shard holds no text at all, only pipes and rules.

    The previous test also dropped anything under 30 characters, which threw
    away real table cells such as ``| 判定规则 |``. Length is no evidence of
    decoration; containing no character at all is.
    """

    return not _LAYOUT_ONLY_RE.sub("", text)


def _is_reference_unit(unit: StructuredUnit) -> bool:
    """References are useful source metadata, not legal-answer evidence."""

    return any(
        re.sub(r"\s+", "", heading).lower() in {"参考文献", "references"}
        for heading in unit.heading_path
    )


def _chunks_from_units(document: Document, units: list[StructuredUnit]) -> list[Chunk]:
    chunks: list[Chunk] = []
    citation_role = citation_role_for_source(document)

    kept_units = [
        unit
        for unit in units
        if unit.text.strip()
        and not _is_layout_shard(unit.text)
        and not _is_reference_unit(unit)
    ]
    for index, unit in enumerate(kept_units):
        heading_path = [document.title, *unit.heading_path]
        citation_label = unit.citation_label
        if citation_label:
            citation_label = f"{document.title} {citation_label}"
        chunks.append(
            Chunk(
                chunk_id=f"{document.doc_id}:{index:04d}",
                doc_id=document.doc_id,
                source_id=document.source_id,
                library_kind=document.library_kind,
                title=document.title,
                text=unit.text.strip(),
                chunk_index=index,
                doc_type=document.doc_type,
                heading_path=heading_path,
                citation_label=citation_label,
                citation_role=citation_role,
                # Structured documents (guides, templates, Q&A, standards) do
                # not carry article numbers and therefore cannot be quoted as
                # concrete clauses. They remain retrievable and appear in the
                # evidence panel — just not as inline clause citations.
                can_cite_clause=False,
                prev_chunk_id=f"{document.doc_id}:{index - 1:04d}" if index > 0 else None,
                next_chunk_id=(
                    f"{document.doc_id}:{index + 1:04d}" if index + 1 < len(kept_units) else None
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
                char_count=len(unit.text.strip()),
            )
        )
    return chunks


def _meaningful_lines(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.lower() in {"<!-- image -->", "<!--image-->"}:
            continue
        lines.append(stripped)
    return lines


def _looks_table_heavy(text: str) -> bool:
    """Return True if the text contains HTML or markdown tables.

    Markdown tables are detected by the presence of a separator row
    (``|---|---|``) followed by at least one data row. This catches
    tables produced by docling/TableFormerV2 which export as markdown.
    """
    if "<table" in text.lower() or text.count("<tr") >= 3:
        return True
    # Markdown table: separator row + at least 2 other `|`-delimited rows
    sep_count = len(MARKDOWN_TABLE_SEP_RE.findall(text))
    row_count = len(MARKDOWN_TABLE_ROW_RE.findall(text))
    return sep_count >= 1 and row_count >= 3


def _heading_from_line(line: str) -> tuple[int, str] | None:
    stripped = line.strip()
    appendix = APPENDIX_HEADING_RE.match(stripped)
    if appendix:
        value = re.sub(r"^#{1,6}\s+", "", stripped).strip()
        return 1, value
    markdown = MARKDOWN_HEADING_RE.match(stripped)
    if markdown:
        value = markdown.group(2).strip()
        return _heading_level(value, min(len(markdown.group(1)), 6)), value
    appendix_subheading = APPENDIX_SUBHEADING_RE.match(stripped)
    if appendix_subheading:
        level = appendix_subheading.group(2).count(".") + 2
        value = f"{appendix_subheading.group(1)}.{appendix_subheading.group(2)} {appendix_subheading.group(3).strip()}"
        return min(level, 6), value
    numeric = NUMERIC_HEADING_RE.match(stripped)
    numeric_title = numeric.group(2).strip() if numeric else ""
    # A title with a sentence mark inside it is a sentence: a numbered table row
    # or a wrapped body line ("4 不满 1 万人敏感个人信息。视频、图像…") starts
    # with a digit just as a heading does. Headings are noun phrases, so only a
    # mark at the very end can be a heading's own punctuation.
    is_list_item = (
        bool(re.match(r"^(?:[a-zA-Z]|\d+)\)", numeric_title))
        or numeric_title.endswith((":", "："))
        or bool(re.search(r"[。；]", numeric_title))
    )
    if numeric and not is_list_item:
        # The line is published as it was written. Rebuilding it from the number
        # and the title dropped the separator the source put between them, and
        # the coverage check then read a body line as lost because every chunk
        # held a version without its `.`.
        level = numeric.group(1).count(".") + 1
        return min(level, 6), stripped
    if re.match(r"^[一二三四五六七八九十]+、.{1,80}$", stripped):
        # An item that ends with sentence punctuation states a requirement in
        # its own right: it is body text under its section, not a heading. Same
        # rule as the bracket and numeric list-item guards below.
        if not stripped.endswith(("；", "。")):
            return 1, stripped
        return None
    if re.match(r"^（[一二三四五六七八九十]+）.{1,80}$", stripped):
        # A bracketed item ending with sentence punctuation states a requirement
        # in its own right: it is content under its section, not an enumerator
        # heading. Same rule as the numeric list-item guard above.
        if not stripped.endswith(("；", "。")):
            return 2, stripped
        return None
    if stripped.startswith("行业领域") and len(stripped) <= 80:
        return 1, stripped
    return None


def _strip_markdown_heading(line: str) -> str:
    markdown = MARKDOWN_HEADING_RE.match(line.strip())
    return markdown.group(2).strip() if markdown else line.strip()


def _clean_inline_markup(line: str) -> str:
    line = line.strip()
    line = re.sub(r"\*{4,}", "", line)  # empty bold runs left by converters, e.g. "2****.****已****通过"
    line = re.sub(r"^\*{1,3}(.+?)\*{1,3}$", r"\1", line)
    line = re.sub(r"\*\*(问|答|Q|A|问题)\s*[:：]\*\*", r"\1：", line)
    return line.strip()


def _last_context_heading(text: str, fallback: list[str]) -> list[str]:
    path = fallback
    for line in _meaningful_lines(text):
        heading = _heading_from_line(line)
        if heading:
            path = _push_heading(path, *heading)
    return path


def _table_row_text(row_html: str) -> str:
    """Render one table row, keeping empty cells so column positions survive.

    Dropping empty cells shifted every later value one column to the left, which
    silently re-attributed a data item to the wrong category — the columns are
    legal meaning here, so an empty cell is kept as an empty cell rather than
    inferred away. A row where every cell is empty is layout, not content, and
    renders as "" for the caller to drop.
    """

    cells = [_strip_tags(cell.group(1)) for cell in CELL_RE.finditer(row_html)]
    if not any(cells):
        return ""
    return " | ".join(cells)


def _strip_tags(value: str) -> str:
    text = TAG_RE.sub(" ", value)
    text = unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _split_long_text(
    text: str, heading_path: list[str], citation_label: str | None
) -> list[StructuredUnit]:
    lines = _meaningful_lines(text)
    if not lines:
        return []

    units: list[StructuredUnit] = []
    current: list[str] = []

    def flush() -> None:
        nonlocal current
        if current:
            units.append(
                StructuredUnit(
                    text="\n".join(current),
                    heading_path=heading_path,
                    citation_label=citation_label,
                )
            )
            current = []

    for line in lines:
        if current and len("\n".join([*current, line])) > GENERIC_HARD_LIMIT_CHARS:
            flush()
        if len(line) > GENERIC_HARD_LIMIT_CHARS:
            for start in range(0, len(line), GENERIC_SOFT_LIMIT_CHARS):
                units.append(
                    StructuredUnit(
                        text=line[start : start + GENERIC_SOFT_LIMIT_CHARS],
                        heading_path=heading_path,
                        citation_label=citation_label,
                    )
                )
            continue
        current.append(line)
    flush()
    return units


def _merge_tiny_units(units: list[StructuredUnit]) -> list[StructuredUnit]:
    merged: list[StructuredUnit] = []
    for unit in units:
        if (
            merged
            and len(unit.text) < MIN_GENERIC_CHUNK_CHARS
            and merged[-1].heading_path == unit.heading_path
            and len(merged[-1].text) + len(unit.text) <= GENERIC_HARD_LIMIT_CHARS
        ):
            previous = merged[-1]
            merged[-1] = StructuredUnit(
                text=f"{previous.text}\n{unit.text}",
                heading_path=previous.heading_path,
                citation_label=previous.citation_label,
            )
        else:
            merged.append(unit)
    return merged
