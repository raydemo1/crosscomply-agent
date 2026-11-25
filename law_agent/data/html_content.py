"""The main text of an HTML page, without the site around it.

A government page downloaded as HTML carries its navigation, account links,
breadcrumbs, share buttons and footer around one article. Read line by line,
all of that reaches the index as chunks. The page's structure says where the
article is: drop elements that are chrome by tag (``nav``, ``header``,
``footer``, forms, scripts) or by their ``class``/``id`` (``nav``, ``menu``,
``footer``, ``share``, ``breadcrumb``…), then keep the smallest element that
still holds most of the page's prose. Tables are emitted as ``<table>`` rows
so the table-aware chunker reads them as tables.

Responsive pages carry the article twice (a desktop copy and a mobile copy,
one of them hidden by CSS), so a line already emitted is not emitted again.
A notice whose instrument is an attached file names it in a link: those links
are read from the same main element (:func:`attachment_links`).

Standard library only (``html.parser``), tolerant of unclosed tags.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin, urlsplit

SKIP_TAGS = frozenset({
    "script", "style", "noscript", "iframe", "svg", "nav", "header", "footer", "form", "select",
    "button", "input", "textarea", "head", "template", "object", "canvas", "map",
})
VOID_TAGS = frozenset({
    "br", "img", "meta", "link", "input", "hr", "area", "base", "col", "embed", "param", "source",
    "track", "wbr",
})
BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "main", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
    "table", "tbody", "thead", "tr", "blockquote", "pre", "dd", "dt", "dl", "center", "body", "html",
})
# A class or id naming page chrome, as a whole word of the name ("main-nav", "footer_box", "share").
CHROME_NAME_RE = re.compile(
    r"(?:^|[-_\s])(?:nav|navbar|menu|header|footer|foot|topbar|top|bottom|share|breadcrumb|crumb|crumbs"
    r"|position|location|toolbar|tools|sidebar|side|search|login|links?|copyright|banner|logo|qrcode|qr"
    r"|weixin|wechat|weibo|erweima|fontsize|print|related|recommend|ad|ads)(?:$|[-_\s\d])",
    re.IGNORECASE,
)
PROSE_MIN_CHARS = 15
MAIN_SHARE = 0.8
# A repeated line this long is the page's second copy of the article, not a repeated phrase.
DUPLICATE_LINE_MIN_CHARS = 12
ATTACHMENT_SUFFIXES = frozenset({".pdf", ".docx", ".doc", ".wps", ".ofd", ".xls", ".xlsx", ".zip", ".rar", ".txt"})


@dataclass
class _Node:
    tag: str
    attrs: dict[str, str]
    children: list[_Node | str] = field(default_factory=list)
    parent: _Node | None = None

    @property
    def chrome(self) -> bool:
        if self.tag in SKIP_TAGS:
            return True
        names = " ".join(filter(None, (self.attrs.get("class"), self.attrs.get("id"))))
        return bool(names and CHROME_NAME_RE.search(names))


class _TreeBuilder(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("root", {})
        self.current = self.root

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = _Node(tag, {name: value or "" for name, value in attrs}, parent=self.current)
        self.current.children.append(node)
        if tag not in VOID_TAGS:
            self.current = node

    def handle_endtag(self, tag: str) -> None:
        node = self.current
        while node is not None and node.tag != tag:
            node = node.parent
        if node is not None and node.parent is not None:
            self.current = node.parent  # closes any tags left open inside it

    def handle_data(self, data: str) -> None:
        self.current.children.append(data)


def _prose(node: _Node) -> int:
    """Characters in runs long enough to be text rather than labels, outside chrome."""

    total = 0
    for child in node.children:
        if isinstance(child, str):
            text = child.strip()
            total += len(text) if len(text) >= PROSE_MIN_CHARS else 0
        elif not child.chrome:
            total += _prose(child)
    return total


def _main_node(root: _Node) -> _Node:
    """The smallest element that still holds most of the page's prose."""

    total = _prose(root)
    if not total:
        return root
    node = root
    while True:
        best = max((child for child in node.children if isinstance(child, _Node) and not child.chrome),
                   key=_prose, default=None)
        if best is None or _prose(best) < MAIN_SHARE * total:
            return node
        node = best


def _inline(node: _Node) -> str:
    parts: list[str] = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(child)
        elif not child.chrome:
            parts.append("\n" if child.tag in {"br", "p", "div", "li"} else "")
            parts.append(_inline(child))
    return re.sub(r"[ \t\r\f\v ]+", " ", "".join(parts))


def _render(node: _Node, out: list[str]) -> None:
    for child in node.children:
        if isinstance(child, str):
            out.append(child)
            continue
        if child.chrome:
            continue
        if child.tag == "table":
            rows = []
            for row in _descendants(child, "tr"):
                cells = [_inline(cell).strip() for cell in row.children
                         if isinstance(cell, _Node) and cell.tag in {"td", "th"}]
                if any(cells):
                    rows.append("<tr>" + "".join(f"<td>{escape(cell)}</td>" for cell in cells) + "</tr>")
            if rows:
                out.append("\n<table>" + "".join(rows) + "</table>\n")
            continue
        if child.tag == "br":
            out.append("\n")
            continue
        block = child.tag in BLOCK_TAGS
        if block:
            out.append("\n")
        _render(child, out)
        if block:
            out.append("\n")


def _descendants(node: _Node, tag: str) -> list[_Node]:
    found: list[_Node] = []
    for child in node.children:
        if isinstance(child, _Node):
            if child.tag == tag:
                found.append(child)
            elif child.tag != "table":  # rows of a nested table belong to that table
                found.extend(_descendants(child, tag))
    return found


def _tree(raw: str) -> _Node:
    builder = _TreeBuilder()
    builder.feed(raw)
    builder.close()
    return builder.root


def extract_main_text(raw: str) -> str:
    out: list[str] = []
    _render(_main_node(_tree(raw)), out)
    seen: set[str] = set()
    lines: list[str] = []
    for line in "".join(out).splitlines():
        line = re.sub(r"[ \t\u00a0]+", " ", line).strip()
        if len(line) >= DUPLICATE_LINE_MIN_CHARS:
            if line in seen:
                continue
            seen.add(line)
        lines.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def attachment_links(raw: str, base_url: str) -> list[tuple[str, str]]:
    """``(name, absolute url)`` of each file the page's main text links to, in page order, each once."""

    found: dict[str, str] = {}
    for anchor in _descendants_all(_main_node(_tree(raw)), "a"):
        href = anchor.attrs.get("href", "").strip()
        if not href or href.startswith(("javascript:", "mailto:", "#")):
            continue
        url = urljoin(base_url, href)
        path = PurePosixPath(unquote(urlsplit(url).path))
        if path.suffix.lower() not in ATTACHMENT_SUFFIXES or url in found:
            continue
        found[url] = re.sub(r"\s+", " ", _inline(anchor)).strip() or path.name
    return [(name, url) for url, name in found.items()]


def _descendants_all(node: _Node, tag: str) -> list[_Node]:
    found: list[_Node] = []
    for child in node.children:
        if isinstance(child, _Node) and not child.chrome:
            if child.tag == tag:
                found.append(child)
            found.extend(_descendants_all(child, tag))
    return found
