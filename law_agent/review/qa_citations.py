"""Tool-owned source markers and presentation references for Q&A answers."""

import hashlib
import re

MARKER_START = "[[cite:"
MARKER_RE = re.compile(r"\[\s*\[\s*cite\s*:\s*([^\]]*?)\s*\]\s*\]", re.IGNORECASE)
LOOSE_MARKER_RE = re.compile(r"\[\s*(?:\[\s*)?cite\s*:\s*([^\]]*?)\s*\](?:\s*\])?", re.IGNORECASE)
RESERVED_MARKER_RE = re.compile(r"\[\s*(?:\[\s*)?cite\s*:", re.IGNORECASE)
PARTIAL_MARKER_RE = re.compile(r"\[(?:\s*\[)?\s*(?:c|ci|cit|cite\s*)?\Z", re.IGNORECASE)
DISPLAY_REF_RE = re.compile(r"\[依据[^\]]*\]")


class CitationReferenceError(ValueError):
    def __init__(self, message: str, references: list[str] | None = None):
        super().__init__(message)
        self.references = list(dict.fromkeys(references or []))


def citation_marker(identity: str, *, length: int = 8) -> str:
    return f"{MARKER_START}{hashlib.sha256(identity.encode()).hexdigest()[:length]}]]"


def register_citation_marker(identity: str, markers: dict[str, str]) -> str:
    if identity not in markers:
        used = set(markers.values())
        for length in range(8, 65, 4):
            marker = citation_marker(identity, length=length)
            if marker not in used:
                markers[identity] = marker
                break
        else:
            raise ValueError("引用来源标识冲突")
    return markers[identity]


def _marker(identity: str) -> str:
    if re.fullmatch(r"[0-9a-f\s]+", identity, re.IGNORECASE):
        identity = re.sub(r"\s+", "", identity).lower()
    return f"{MARKER_START}{identity}]]"


def citation_repair_tool(error: CitationReferenceError, citations: list[dict]) -> dict:
    return {"type": "function", "function": {
        "name": "repair_citations", "strict": True,
        "description": "仅选择已取得且支持原段落的引用，修复给定的无效标记。不得修改正文或猜测来源；不能确定时返回空数组。",
        "parameters": {"type": "object", "additionalProperties": False, "required": ["replacements"],
            "properties": {"replacements": {"type": "array", "items": {
                "type": "object", "additionalProperties": False, "required": ["old", "new"],
                "properties": {"old": {"type": "string", "enum": error.references},
                               "new": {"type": "string", "enum": [c["citation_marker"] for c in citations]}}
            }}}}
    }}


def apply_citation_repairs(text: str, error: CitationReferenceError, citations: list[dict], replacements: list) -> str:
    allowed = {c["citation_marker"] for c in citations}
    edits = {}
    if not error.references or not isinstance(replacements, list):
        raise CitationReferenceError("引用修复补丁无效")
    for item in replacements:
        if (not isinstance(item, dict) or set(item) != {"old", "new"}
                or not isinstance(item["old"], str) or not isinstance(item["new"], str)
                or item["old"] not in error.references or item["new"] not in allowed or item["old"] in edits):
            raise CitationReferenceError("引用修复补丁包含无效来源或重复替换")
        edits[item["old"]] = item["new"]
    if set(edits) != set(error.references):
        raise CitationReferenceError("引用修复未覆盖全部无效标记", error.references)
    pattern = re.compile("|".join(re.escape(key) for key in sorted(edits, key=len, reverse=True)))
    return pattern.sub(lambda match: edits[match[0]], text)


def render_qa_citations(text: str, citations: list[dict], *, final: bool = True) -> tuple[str, list[dict]]:
    citations = [{**c, "kind": "context", "can_cite_clause": False}
                 if c.get("available_at_answer_date") is False else c for c in citations]
    by_marker = {c["citation_marker"]: c for c in citations}
    aliases: dict[str, set[str]] = {}
    for citation in citations:
        chunk_id = citation.get("chunk_id")
        if chunk_id:
            for identity in {chunk_id, chunk_id.rsplit(":", 1)[-1]}:
                aliases.setdefault(_marker(identity), set()).add(citation["citation_marker"])
    lookup = dict(by_marker)
    for alias, markers in aliases.items():
        if len(markers) == 1 and alias not in lookup:
            lookup[alias] = by_marker[next(iter(markers))]
    if final:
        def normalize(match):
            marker = _marker(match[1])
            return lookup[marker]["citation_marker"] if marker in lookup else match[0]

        text = LOOSE_MARKER_RE.sub(normalize, text)
        trailing = re.search(r"\[\s*(?:\[\s*)?cite\s*:\s*([0-9a-f\s]+)\Z", text, re.IGNORECASE)
        if trailing and _marker(trailing[1]) in lookup:
            text = text[:trailing.start()] + lookup[_marker(trailing[1])]["citation_marker"]
    if DISPLAY_REF_RE.search(text):
        if final:
            raise CitationReferenceError("不要编写引用编号，请复制工具提供的完整引用标记",
                                         DISPLAY_REF_RE.findall(text))
        text = DISPLAY_REF_RE.sub("", text)
    invalid = [match[0] for match in LOOSE_MARKER_RE.finditer(text) if _marker(match[1]) not in lookup]
    if final and invalid:
        raise CitationReferenceError("回答包含未取得的引用标记", invalid)
    used = set()

    def replace(match):
        marker = _marker(match[1])
        if marker not in lookup:
            if final:
                raise CitationReferenceError("回答包含未取得的引用标记", [match[0]])
            return ""
        citation = lookup[marker]
        used.add(citation["ref"])
        suffix = "（非回答时点有效依据，仅供比较）" if citation.get("available_at_answer_date") is False else ""
        return f"[{citation['ref']}]{suffix}"

    answer = MARKER_RE.sub(replace, text)
    incomplete = RESERVED_MARKER_RE.search(answer) or PARTIAL_MARKER_RE.search(answer)
    if incomplete:
        if final and ("cite" in incomplete[0].lower() or re.match(r"\[\s*\[", incomplete[0])):
            raise CitationReferenceError("回答包含不完整的引用标记")
        if not final:
            answer = answer[:incomplete.start()]
            used = {ref for ref in used if f"[{ref}]" in answer}
    selected = [{key: value for key, value in c.items() if key != "citation_marker"}
                for c in citations if c["ref"] in used]
    return answer, selected
