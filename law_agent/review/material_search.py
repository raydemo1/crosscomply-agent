from __future__ import annotations

import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import PurePath

from pydantic import Field, model_validator

from law_agent.data.schemas import StrictModel
from law_agent.review.enterprise_store import MaterialVersion
from law_agent.review.materials import SHEET_HEADING
from law_agent.review.tool_calling import ToolSpec

INLINE_BYTES = 8000
READ_BYTES = 48000
GREP_BYTES = 6000
GREP_MATCHES = 8


@dataclass(frozen=True)
class MaterialFile:
    id: str
    filename: str
    text: str
    version_number: int | None = None
    inline: bool = False
    logical_name: str | None = None

    @cached_property
    def format(self) -> str:
        return PurePath(self.filename).suffix.lower().lstrip(".") or "txt"

    @cached_property
    def sheets(self) -> dict[str, tuple[int, int]]:
        if self.format != "xlsx":
            return {}
        headings = list(
            re.finditer(r"(?m)^" + re.escape(SHEET_HEADING) + r'("(?:[^"\\]|\\.)*")$', self.text)
        )
        ends = [match.start() for match in headings[1:]] + [len(self.text)]
        return {json.loads(match[1]): (match.start(), end) for match, end in zip(headings, ends)}

    @classmethod
    def from_version(cls, version: MaterialVersion) -> MaterialFile:
        return cls(
            id=version.id,
            filename=version.filename,
            text=version.parsed_text or "",
            version_number=version.version_number,
            inline=version.logical_name == "材料正文" and version.filename == "case-material.txt",
            logical_name=version.logical_name,
        )

    @classmethod
    def from_qa(cls, record: dict) -> MaterialFile:
        filename = record["filename"]
        return cls(record["id"], filename, record["parsed_text"])


class MaterialReadRequest(StrictModel):
    material_version_id: str = Field(
        min_length=1, max_length=200, description="材料清单中的版本 ID"
    )
    start: int | None = Field(default=None, ge=0, description="文件内的起始字符位置")
    end: int | None = Field(default=None, ge=0, description="文件内的结束字符位置，不含该字符")
    sheet: str | None = Field(
        default=None, min_length=1, max_length=100, description="XLSX 工作表名称"
    )

    @model_validator(mode="after")
    def valid_range(self) -> MaterialReadRequest:
        if self.end is not None and self.start is not None and self.end < self.start:
            raise ValueError("end 不能小于 start")
        return self


class MaterialToolArguments(StrictModel):
    material_requests: list[MaterialReadRequest] = Field(default_factory=list, max_length=8)
    pattern: str = Field(default="", max_length=200, description="要查找的原文字符串")
    material_version_ids: list[str] = Field(
        default_factory=list, max_length=30, description="限定材料版本；省略时搜索本轮材料"
    )


MATERIAL_FILE_TOOLS = {
    "read_materials": ToolSpec(
        ("material_requests",),
        "按材料版本批量读取原文。可选择工作表或字符范围；省略时读取全文。",
        ("material_requests",),
    ),
    "grep_materials": ToolSpec(
        ("pattern", "material_version_ids"),
        "在本轮材料中查找字面字符串，返回位置和附近原文。",
        ("pattern",),
    ),
}


def _header(version: MaterialVersion) -> str:
    return f"【材料 {version.logical_name} v{version.version_number} | {version.id}】\n"


def format_frozen_material(versions: Sequence[MaterialVersion]) -> str:
    return "\n\n".join(_header(version) + (version.parsed_text or "") for version in versions)


def material_inventory(files: Sequence[MaterialFile]) -> list[dict]:
    return [
        {
            "material_version_id": file.id,
            "filename": file.filename,
            "version_number": file.version_number,
            "format": file.format,
            "characters": len(file.text),
            **({"logical_name": file.logical_name} if file.logical_name else {}),
            **(
                {
                    "sheets": list(file.sheets),
                    "representation": "每行一个 JSON 数组，公式使用已保存的计算值",
                }
                if file.sheets
                else {}
            ),
        }
        for file in files
    ]


def inline_material_passages(files: Sequence[MaterialFile]) -> list[dict]:
    remaining = INLINE_BYTES
    passages = []
    for file in files:
        if file.inline and file.text:
            size = len(file.text.encode("utf-8"))
            if size <= remaining:
                passages.append(
                    {
                        "material_version_id": file.id,
                        "filename": file.filename,
                        "offset": 0,
                        "text": file.text,
                    }
                )
                remaining -= size
    return passages


def _byte_prefix(text: str, limit: int) -> str:
    return text[:limit].encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def read_materials(files: Sequence[MaterialFile], requests: Sequence[MaterialReadRequest]) -> dict:
    if not requests:
        raise ValueError("请选择要读取的材料版本")
    by_id = {file.id: file for file in files}
    results = []
    windows = []
    for request in requests:
        file = by_id.get(request.material_version_id)
        if file is None:
            results.append(
                {"material_version_id": request.material_version_id, "error": "材料不在本轮范围内"}
            )
            continue
        bounds = (0, len(file.text)) if request.sheet is None else file.sheets.get(request.sheet)
        if bounds is None:
            results.append({"material_version_id": file.id, "error": "工作表不存在"})
            continue
        left, right = bounds
        start = left if request.start is None else request.start
        stop = right if request.end is None else request.end
        if not left <= start <= stop <= right:
            results.append(
                {
                    "material_version_id": file.id,
                    "error": "读取范围超出原文",
                    "available_range": {"start": left, "end": right},
                }
            )
            continue
        sample = _byte_prefix(file.text[start : min(stop, start + READ_BYTES)], READ_BYTES)
        item = {"material_version_id": file.id, "filename": file.filename, "offset": start}
        if request.sheet is not None:
            item["sheet"] = request.sheet
        results.append(item)
        windows.append((item, request, stop, sample))
    remaining = READ_BYTES
    reserve = sum(len(sample.encode("utf-8")) for _, _, _, sample in windows)
    for index, (item, request, stop, sample) in enumerate(windows):
        reserve -= len(sample.encode("utf-8"))
        limit = max(remaining - reserve, remaining // (len(windows) - index))
        text = _byte_prefix(sample, limit)
        remaining -= len(text.encode("utf-8"))
        end = item["offset"] + len(text)
        item.update(
            text=text,
            end=end,
            truncated=end < stop,
            next_read=request.model_dump(exclude_none=True) | {"start": end}
            if end < stop
            else None,
        )
    return {"passages": results}


def grep_materials(
    files: Sequence[MaterialFile], pattern: str, version_ids: Sequence[str] = ()
) -> dict:
    if not pattern.strip():
        raise ValueError("请输入要查找的原文字符串")
    selected = set(version_ids)
    if selected - {file.id for file in files}:
        raise ValueError("指定了本轮范围之外的材料")
    matcher = re.compile(re.escape(pattern), re.IGNORECASE)
    hits = []
    remaining = GREP_BYTES
    for file in files:
        if selected and file.id not in selected:
            continue
        last_end = 0
        for match in matcher.finditer(file.text):
            if match.end() <= last_end:
                continue
            if len(hits) == GREP_MATCHES or remaining < len(match.group().encode("utf-8")):
                return {"hits": hits, "truncated": True}
            start = max(min(last_end, match.start()), match.start() - 80)
            prefix_bytes = len(file.text[start : match.end()].encode("utf-8"))
            if prefix_bytes > remaining:
                start = match.start()
            text = _byte_prefix(file.text[start : match.end() + 160], remaining)
            last_end = start + len(text)
            remaining -= len(text.encode("utf-8"))
            hits.append(
                {
                    "material_version_id": file.id,
                    "filename": file.filename,
                    "offset": start,
                    "match_start": match.start(),
                    "match_end": match.end(),
                    "text": text,
                }
            )
    return {"hits": hits, "truncated": False}
