"""Material input normalization for review cases."""

from __future__ import annotations

import json
from pathlib import Path
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile, ZipFile

from defusedxml.common import DefusedXmlException

from law_agent.data.normalize import ParserEngine, normalize_source
from law_agent.data.schemas import SourceRecord
from law_agent.review.schemas import MaterialRecord, UploadedFileMeta

SHEET_HEADING = "## 工作表 "
MAX_WORKBOOK_TEXT_BYTES = 8 * 1024 * 1024


def _workbook_text(path: Path) -> MaterialRecord:
    from openpyxl import __version__, load_workbook
    from openpyxl.utils.exceptions import InvalidFileException

    lines: list[str] = []
    size = 0

    def add(line: str) -> None:
        nonlocal size
        size += len(line.encode("utf-8")) + 1
        if size > MAX_WORKBOOK_TEXT_BYTES:
            raise ValueError("工作簿解析后的文本超过 8 MiB，请拆分工作表后上传")
        lines.append(line)

    try:
        with ZipFile(path) as archive:
            if sum(entry.file_size for entry in archive.infolist()) > 128 * 1024 * 1024:
                raise ValueError("工作簿解压后的大小超过 128 MiB")
        workbook = load_workbook(path, read_only=True, data_only=True, keep_links=False)
        try:
            for sheet in workbook.worksheets:
                add(SHEET_HEADING + json.dumps(sheet.title, ensure_ascii=False))
                sheet.reset_dimensions()
                for row in sheet.iter_rows(values_only=True):
                    add(json.dumps(row, ensure_ascii=False, default=str))
        finally:
            workbook.close()
    except (BadZipFile, DefusedXmlException, InvalidFileException, KeyError, OSError, ParseError) as exc:
        raise ValueError("不是可读取的 XLSX 工作簿") from exc
    return MaterialRecord(
        input_mode="uploaded_file", material_text="\n".join(lines), source_name=path.name,
        parser="openpyxl", parser_version=__version__,
        uploaded_file=UploadedFileMeta(filename=path.name, local_path=str(path), raw_format="xlsx"),
    )


def material_from_text(material_text: str) -> MaterialRecord:
    return MaterialRecord(material_text=material_text)


def material_from_file(path: Path, *, parser: ParserEngine = "auto") -> MaterialRecord:
    if not path.exists():
        raise FileNotFoundError(f"material file does not exist: {path}")
    if not path.is_file():
        raise ValueError(f"material path is not a file: {path}")

    resolved = path.resolve()
    raw_format = resolved.suffix.lower().lstrip(".") or "txt"
    if raw_format == "xlsx":
        return _workbook_text(resolved)
    record = SourceRecord(
        source_id=f"upload_{resolved.stem}",
        title=resolved.stem,
        source_url=resolved.as_uri(),
        source_site="user_upload",
        doc_type="internal_policy",
        file_format=raw_format,
        include_in_mvp=True,
    )
    document = normalize_source(record, resolved, parser=parser)
    return MaterialRecord(
        input_mode="uploaded_file",
        material_text=document.text,
        source_name=resolved.name,
        parser=document.ingest_meta.parser,
        parser_version=document.ingest_meta.parser_version,
        uploaded_file=UploadedFileMeta(
            filename=resolved.name,
            local_path=str(resolved),
            raw_format=document.raw_format,
        ),
    )
