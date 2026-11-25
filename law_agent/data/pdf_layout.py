"""Read a PDF's text layer with its layout: tables as tables, wrapped lines rejoined.

A plain text-layer read flattens a table into lines laid out column by column,
so a negative-list row (data category | data item | scope) comes out as
fragments of three cells interleaved across lines, and its row number lands in
one chunk while its text lands in the next. The table cell grid is in the PDF,
so it is read as a grid here:

* table regions become ``<table>`` rows in reading order with the text around
  them;
* a cell covered by a vertical merge (``None`` in the grid) takes the value of
  the cell above it, which is what the merge means on the page;
* a table that runs over a page break continues as one table: the repeated
  header row is dropped and a row split by the break is stitched back;
* body lines wrapped at the right margin are rejoined, and the spaces a PDF
  puts between CJK glyphs are removed, so a word such as 负面清单 is not split
  in the index.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

_CJK = "㐀-䶿一-鿿豈-﫿"
_CJK_PUNCT = "　-〿！-／：-＠［-｀｛-･—…“”‘’"
# A space between two CJK glyphs (or CJK punctuation) never carries meaning in Chinese text.
_CJK_GAP_RE = re.compile(rf"(?<=[{_CJK}{_CJK_PUNCT}])[  　]+(?=[{_CJK}{_CJK_PUNCT}])")
# Between CJK and a number ("提供 10 万人") the space is typographic too; a leading section number
# ("3.12 转让") is protected by the caller.
_CJK_DIGIT_GAP_RE = re.compile(rf"(?<=[{_CJK}])[  ]+(?=\d)|(?<=\d)[  ]+(?=[{_CJK}])")
_LEADING_NUMBER_RE = re.compile(r"^((?:[A-Z]\.)?\d+(?:\.\d+)*\s+|第[一二三四五六七八九十百千零〇\d]+[条章节]\s+)")
_PAGE_NUMBER_RE = re.compile(r"^[-—–－─━\s]*\d{1,3}[-—–－─━\s]*$")
# A line that opens a new structural element is never the tail of the line before it.
_BLOCK_START_RE = re.compile(
    r"^(?:第[一二三四五六七八九十百千零〇\d]+[编章节条]|[一二三四五六七八九十]+、|（[一二三四五六七八九十\d]+）"
    r"|\([一二三四五六七八九十\d]+\)|\d+(?:\.\d+)*[\.、．]?\s|\d+[\.、．]|[①-⑳]|注\s*\d*[:：]|附件|附录|[A-Z]\.\d)"
)
_SENTENCE_END = tuple("。；：！？”）)】」;:!?")
_ROW_NUMBER_RE = re.compile(r"^\d+(?:[\.、．]|$)")
_CELL_ITEM_RE = re.compile(r"^(?:\d+[\.、．]|（\d+）|\(\d+\)|[①-⑳]|[一二三四五六七八九十]+、)")


def normalize_spacing(line: str) -> str:
    match = _LEADING_NUMBER_RE.match(line)
    head, rest = (match.group(1), line[match.end():]) if match else ("", line)
    rest = _CJK_GAP_RE.sub("", rest)
    rest = _CJK_DIGIT_GAP_RE.sub("", rest)
    return head + rest


def _cell_text(value: str) -> str:
    """Rejoin the lines a cell was wrapped into, keeping a break before each list item."""

    parts = [normalize_spacing(part.strip()) for part in value.splitlines() if part.strip()]
    text = ""
    for part in parts:
        text = f"{text}\n{part}" if text and _CELL_ITEM_RE.match(part) else text + part
    return text


@dataclass
class _Table:
    ncols: int
    rows: list[list[str]] = field(default_factory=list)
    header: list[str] | None = None


def _is_title_row(row: list[str | None]) -> bool:
    return row[0] not in (None, "") and all(cell is None for cell in row[1:])


def _is_header_row(row: list[str]) -> bool:
    cells = [cell for cell in row if cell]
    return (len(cells) >= 2 and all(len(cell) <= 20 and not re.search(r"[。；]", cell)
                                    and not re.match(r"^\d", cell) for cell in cells))


def _add_rows(table: _Table, grid: list[list[str | None]], *, continued: bool) -> None:
    page_top = continued  # until the first row of this page that is neither a title nor a repeated header
    for raw in grid:
        if _is_title_row(raw):
            table.rows.append([_cell_text(raw[0] or "")])
            table.header = None
            continue
        row = [None if cell is None else _cell_text(cell) for cell in raw]
        filled = [cell or "" for cell in row]
        if _is_header_row(filled):
            if page_top and filled == table.header:
                continue  # the header a table repeats at the top of each page
            table.header = filled
            table.rows.append(filled)
            continue
        # Only the row directly above can be continued or merged into: a title row in between starts a new section.
        previous = table.rows[-1] if table.rows and len(table.rows[-1]) == len(row) else None
        at_page_top, page_top = page_top, False
        if (at_page_top and previous is not None and row[0] == "" and any(row[1:])
                and not any(_ROW_NUMBER_RE.match(cell or "") for cell in row)):
            # The page break split this row: no cell opens a new numbered row, so the cells continue the row above.
            previous[:] = [p + (c or "") for p, c in zip(previous, row, strict=True)]
            continue
        if previous is not None and previous is not table.header:
            # A vertical merge: the cell belongs to the one above it. At the top of a page the merged cell
            # comes out empty rather than absent, because the merge started on the page before.
            row = [p if c is None or (at_page_top and c == "") else c for p, c in zip(previous, row, strict=True)]
        table.rows.append([cell or "" for cell in row])


def _table_html(table: _Table) -> str:
    rows = ["<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in row) + "</tr>" for row in table.rows if any(row)]
    return "<table>" + "".join(rows) + "</table>"


def _join_lines(lines: list[dict]) -> list[str]:
    """Rejoin lines wrapped at the right margin into the paragraphs they came from."""

    if not lines:
        return []
    # The body's right margin: most full lines end there. A running header or a wide formula can end further
    # right, so the margin is the edge a good share of lines reach, not the furthest one.
    edges = sorted(line["x1"] for line in lines)
    right = edges[int(len(edges) * 0.8)]
    out: list[str] = []
    previous: dict | None = None
    for line in lines:
        text = normalize_spacing(line["text"].strip())
        if not text or _PAGE_NUMBER_RE.match(text):
            continue
        if previous is not None and out:
            width = (previous["x1"] - previous["x0"]) / max(len(previous["text"]), 1)
            wrapped = (previous["x1"] >= right - 2 * width and not out[-1].endswith(_SENTENCE_END)
                       and not _BLOCK_START_RE.match(text))
            if wrapped:
                out[-1] += text
                previous = line
                continue
        out.append(text)
        previous = line
    return out


def _size(line: dict) -> float:
    sizes = sorted(char["size"] for char in line.get("chars", []))
    return sizes[len(sizes) // 2] if sizes else 0.0


def read_pdf_layout(path: Path) -> str:
    """Text and tables in reading order. Footnotes, set in a smaller type at the foot of a page, are moved
    to the end so a sentence that runs from one page to the next is rejoined across the break."""

    import pdfplumber

    blocks: list[str] = []
    footnotes: list[dict] = []
    pending: list[dict] = []  # body lines carry over a page break, so a wrapped sentence rejoins
    open_table: _Table | None = None

    def flush_lines() -> None:
        nonlocal pending
        blocks.extend(_join_lines(pending))
        pending = []

    def close_table() -> None:
        nonlocal open_table
        if open_table is not None:
            blocks.append(_table_html(open_table))
            open_table = None

    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            tables = page.find_tables()
            boxes = [table.bbox for table in tables]

            def outside(obj, boxes=boxes) -> bool:
                cx, cy = (obj["x0"] + obj["x1"]) / 2, (obj["top"] + obj["bottom"]) / 2
                return not any(x0 <= cx <= x1 and top <= cy <= bottom for x0, top, x1, bottom in boxes)

            lines = [line for line in page.filter(outside).extract_text_lines(keep_blank_chars=False)
                     if line["text"].strip() and not _PAGE_NUMBER_RE.match(line["text"].strip())]
            sizes = sorted(_size(line) for line in lines)
            body = sizes[len(sizes) // 2] if sizes else 0.0
            # A footnote is set smaller than the body and sits below the page's last body line; a note set
            # small inside the body (a standard's "注1：") stays where it is.
            body_bottom = max((line["top"] for line in lines if _size(line) >= 0.8 * body), default=0.0)
            bottom_of_tables = max((table.bbox[3] for table in tables), default=0.0)
            page_notes = [line for line in lines if body and _size(line) < 0.8 * body
                          and line["top"] > max(body_bottom, bottom_of_tables)]
            footnotes += page_notes
            items: list[tuple[float, str, object]] = [(line["top"], "line", line) for line in lines
                                                      if line not in page_notes]
            items += [(table.bbox[1], "table", table) for table in tables]
            items.sort(key=lambda item: item[0])
            first_on_page = True
            for _top, kind, item in items:
                if kind == "line":
                    close_table()
                    pending.append(item)
                    first_on_page = False
                    continue
                flush_lines()
                grid = item.extract()
                if not grid:
                    continue
                ncols = len(grid[0])
                continued = open_table is not None and first_on_page and open_table.ncols == ncols
                if not continued:
                    close_table()
                    open_table = _Table(ncols=ncols)
                _add_rows(open_table, grid, continued=continued)
                first_on_page = False
    flush_lines()
    close_table()
    blocks += _join_lines(footnotes)
    return "\n".join(block for block in blocks if block)
