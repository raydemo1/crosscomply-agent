"""Normalize raw source files into Document records."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape, unescape
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
from pathlib import Path
from typing import Literal
from xml.etree import ElementTree as ET

from law_agent.data.schemas import Document, IngestMeta, SourceRecord

HTML_TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_STYLE_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
BLOCK_CLOSE_RE = re.compile(r"</(p|div|tr|li|h[1-6]|ul|ol|table)\s*>", re.IGNORECASE)
BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
CELL_OPEN_RE = re.compile(r"<(td|th)[^>]*>", re.IGNORECASE)
CELL_CLOSE_RE = re.compile(r"</(td|th)\s*>", re.IGNORECASE)
WORD_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
ParserEngine = Literal["auto", "plain", "docx", "docling", "mineru"]
DOC_PARSER_FORMATS = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}
DEFAULT_DOCLING_ARTIFACTS_PATH = Path("data/models/docling")


def _html_to_text(raw: str) -> str:
    """Convert HTML to readable text while preserving table structure.

    Drops ``<script>``/``<style>`` blocks (which otherwise leak CSS/JS noise
    into the extracted text), inserts newlines for block-level closers, turns
    table cells into ``|``-separated columns, then strips remaining tags and
    unescapes entities. This keeps tables and lists readable for downstream
    chunking instead of concatenating all cell text into one run.
    """
    raw = SCRIPT_STYLE_RE.sub("", raw)
    raw = BR_RE.sub("\n", raw)
    raw = BLOCK_CLOSE_RE.sub("\n", raw)
    # Opening cell tag -> "" , closing cell tag -> " | " so a row becomes
    # "A | B | C | " (clean single pipes) rather than " | A | | B | ".
    raw = CELL_OPEN_RE.sub("", raw)
    raw = CELL_CLOSE_RE.sub(" | ", raw)
    raw = HTML_TAG_RE.sub("", raw)
    raw = unescape(raw)
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


@dataclass(frozen=True)
class ParsedText:
    text: str
    parser: str
    parser_version: str


def _package_version(*distribution_names: str) -> str:
    for distribution_name in distribution_names:
        try:
            return version(distribution_name)
        except PackageNotFoundError:
            continue
    return "unknown"


def _read_text(
    path: Path,
    parser: ParserEngine = "auto",
    parser_output_dir: Path | None = None,
) -> ParsedText:
    suffix = path.suffix.lower()
    if parser == "docling":
        return _docling_to_text(path)
    if parser == "mineru":
        return _mineru_to_text(path, parser_output_dir=parser_output_dir)
    if parser == "docx":
        if suffix != ".docx":
            raise RuntimeError(f"docx parser cannot parse {suffix or 'extensionless'} files")
        return ParsedText(_docx_to_text(path.read_bytes()), "docx_parser", "0.1.0")
    if parser == "plain":
        return ParsedText(
            path.read_text(encoding="utf-8", errors="replace"), "plain_text_parser", "0.1.0"
        )
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        return ParsedText(_json_to_text(data), "json_parser", "0.1.0")
    if suffix == ".docx":
        return ParsedText(_docx_to_text(path.read_bytes()), "docx_parser", "0.1.0")
    if suffix in {".html", ".htm"}:
        raw = path.read_text(encoding="utf-8", errors="replace")
        return ParsedText(_html_to_text(raw), "html_text_parser", "0.1.0")
    if suffix in DOC_PARSER_FORMATS:
        return _docling_to_text(path)
    return ParsedText(
        path.read_text(encoding="utf-8", errors="replace"), "plain_text_parser", "0.1.0"
    )


def _docling_to_text(path: Path) -> ParsedText:
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.datamodel.pipeline_options import (
            PdfPipelineOptions,
            TableStructureV2Options,
        )
        from docling.document_converter import DocumentConverter, PdfFormatOption
    except ImportError as exc:
        raise RuntimeError(
            "Docling parser requires optional dependency: "
            'install with `pip install -e ".[docling]"` or `uv pip install -U docling`.'
        ) from exc
    ocr_engine = _docling_ocr_engine()
    artifacts_path = _docling_artifacts_path(ocr_engine=ocr_engine)
    # Use TableFormerV2 explicitly. docling's default table_structure_options
    # is TableStructureOptions (V1), which expects the legacy
    # ``docling-project--docling-models/model_artifacts/tableformer`` layout.
    # TableFormerV2 (``docling-project--TableFormerV2``) is the current model
    # and the one we ship in the artifacts directory.
    pipeline_options = PdfPipelineOptions(
        artifacts_path=artifacts_path,
        do_ocr=True,
        ocr_options=_docling_ocr_options(ocr_engine),
        do_table_structure=_docling_tableformer_available(artifacts_path),
        table_structure_options=TableStructureV2Options(),
    )
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )

    # Docling's converter is configured for the PDF pipeline only. Standalone
    # images (.png/.jpg/...) would otherwise be routed to the IMAGE format,
    # which tries to download a HuggingFace layout model — that fails offline
    # with LocalEntryNotFoundError. Wrap images into a single-page PDF so they
    # go through the working PDF + OCR pipeline instead.
    convert_target = path
    image_suffixes = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
    temp_pdf: Path | None = None
    if path.suffix.lower() in image_suffixes:
        convert_target, temp_pdf = _wrap_image_as_pdf(path)

    try:
        result = converter.convert(str(convert_target))
    finally:
        if temp_pdf is not None and temp_pdf.exists():
            temp_pdf.unlink()
    return ParsedText(
        text=result.document.export_to_markdown(),
        parser="docling_parser",
        parser_version=_package_version("docling"),
    )


def _wrap_image_as_pdf(path: Path) -> tuple[Path, Path]:
    """Wrap a raster image into a single-page PDF so Docling's PDF pipeline
    (with OCR) can process it. Returns (pdf_path, temp_pdf_to_clean_up).

    Raises RuntimeError if Pillow is unavailable or the image cannot be read.
    """
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Parsing image files requires Pillow: `pip install Pillow`.") from exc
    try:
        img = Image.open(path)
        # Flatten onto white so transparency/alpha does not break PDF export.
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.convert("RGBA").split()[-1])
            img = background
        else:
            img = img.convert("RGB")
        temp_pdf = path.with_suffix(path.suffix + ".pdf")
        img.save(str(temp_pdf), format="PDF", resolution=200.0)
        return temp_pdf, temp_pdf
    except Exception as exc:
        raise RuntimeError(f"无法将图片 {path.name} 转换为 PDF 以供解析：{exc}") from exc


def _docling_ocr_engine() -> str:
    return os.environ.get("LAWAGENT_DOCLING_OCR_ENGINE", "rapidocr").strip().lower()


def _docling_ocr_options(engine: str | None = None):
    engine = engine or _docling_ocr_engine()
    if engine in {"rapidocr", "rapid_ocr", ""}:
        from docling.datamodel.pipeline_options import RapidOcrOptions

        return RapidOcrOptions(lang=["chinese", "english"], backend="onnxruntime")
    if engine in {"kserve", "kserve_v2", "kserve_v2_ocr", "api", "remote"}:
        from docling.datamodel.pipeline_options import KserveV2OcrOptions

        url = os.environ.get("LAWAGENT_DOCLING_OCR_API_URL", "").strip()
        if not url:
            raise RuntimeError(
                "Docling remote OCR requires LAWAGENT_DOCLING_OCR_API_URL when "
                "LAWAGENT_DOCLING_OCR_ENGINE=kserve_v2_ocr."
            )
        headers = _json_env_dict("LAWAGENT_DOCLING_OCR_HEADERS")
        request_parameters = _json_env_dict("LAWAGENT_DOCLING_OCR_REQUEST_PARAMETERS")
        return KserveV2OcrOptions(
            url=url,
            model_name=os.environ.get("LAWAGENT_DOCLING_OCR_MODEL_NAME", "ocr"),
            model_version=os.environ.get("LAWAGENT_DOCLING_OCR_MODEL_VERSION") or None,
            transport=os.environ.get("LAWAGENT_DOCLING_OCR_TRANSPORT", "http"),
            headers=headers,
            timeout=float(os.environ.get("LAWAGENT_DOCLING_OCR_TIMEOUT", "60")),
            use_binary_data=_bool_env("LAWAGENT_DOCLING_OCR_USE_BINARY_DATA", True),
            request_parameters=request_parameters,
            lang=_csv_env("LAWAGENT_DOCLING_OCR_LANG", ["chinese", "english"]),
        )
    raise RuntimeError(f"Unsupported Docling OCR engine: {engine}")


def _json_env_dict(name: str) -> dict[str, object]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise RuntimeError(f"{name} must be a JSON object")  # noqa: TRY004
    return value


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _csv_env(name: str, default: list[str]) -> list[str]:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    return [item.strip() for item in raw.split(",") if item.strip()]


def _docling_artifacts_path(ocr_engine: str = "rapidocr") -> Path | None:
    configured = os.environ.get("LAWAGENT_DOCLING_ARTIFACTS_PATH")
    if configured is not None:
        configured = configured.strip()
        if not configured:
            return None
        path = Path(configured)
        return path if path.exists() else None

    path = DEFAULT_DOCLING_ARTIFACTS_PATH
    if ocr_engine in {"rapidocr", "rapid_ocr", ""} and not _docling_rapidocr_available(path):
        return None
    return path if path.exists() else None


def _docling_rapidocr_available(artifacts_path: Path) -> bool:
    rapidocr_root = artifacts_path / "RapidOcr"
    required = [
        rapidocr_root / "onnx" / "PP-OCRv4" / "det" / "ch_PP-OCRv4_det_mobile.onnx",
        rapidocr_root / "onnx" / "PP-OCRv4" / "cls" / "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
        rapidocr_root / "onnx" / "PP-OCRv4" / "rec" / "ch_PP-OCRv4_rec_mobile.onnx",
        rapidocr_root
        / "paddle"
        / "PP-OCRv4"
        / "rec"
        / "ch_PP-OCRv4_rec_mobile"
        / "ppocr_keys_v1.txt",
        rapidocr_root / "resources" / "fonts" / "FZYTK.TTF",
    ]
    return all(path.exists() for path in required)


def _docling_tableformer_available(artifacts_path: Path | None) -> bool:
    """Check whether the TableFormer model is available locally.

    docling >= 2.x ships TableFormerV2 (repo ``docling-project/TableFormerV2``,
    local folder ``docling-project--TableFormerV2``) which supersedes the
    legacy ``docling-project--docling-models/model_artifacts/tableformer``
    layout. We check the new path first and fall back to the legacy path so
    the function stays compatible with both docling versions.
    """
    if artifacts_path is None:
        return False

    # docling >= 2.x: TableFormerV2 (safetensors + tokenizer + config)
    v2_root = artifacts_path / "docling-project--TableFormerV2"
    v2_required = [
        v2_root / "model.safetensors",
        v2_root / "config.json",
        v2_root / "tokenizer.json",
    ]
    if all(path.exists() for path in v2_required):
        return True

    # Legacy: docling-project--docling-models (old TableFormer fast variant)
    legacy_root = (
        artifacts_path / "docling-project--docling-models" / "model_artifacts" / "tableformer"
    )
    legacy_required = [
        legacy_root / "fast" / "tm_config.json",
        legacy_root / "fast" / "tableformer_fast.safetensors",
    ]
    return all(path.exists() for path in legacy_required)


def _mineru_to_text(path: Path, parser_output_dir: Path | None = None) -> ParsedText:
    executable = shutil.which("mineru")
    if executable is None:
        raise RuntimeError(
            "MinerU parser requires the `mineru` CLI. "
            'Install with `pip install -e ".[mineru]"` or `uv pip install -U "mineru[all]"`.'
        )
    output_dir = parser_output_dir or path.parent / ".parser_artifacts" / "mineru" / path.stem
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [executable, "-p", str(path), "-o", str(output_dir), "-b", "pipeline"]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise RuntimeError(
            "MinerU parser failed with exit code "
            f"{completed.returncode}: {completed.stderr.strip() or completed.stdout.strip()}"
        )
    markdown_path = _find_mineru_markdown(output_dir, path.stem)
    if markdown_path is None:
        raise RuntimeError(f"MinerU parser did not produce a markdown file under {output_dir}")
    return ParsedText(
        text=markdown_path.read_text(encoding="utf-8", errors="replace"),
        parser="mineru_parser",
        parser_version=_package_version("mineru", "magic-pdf"),
    )


def _find_mineru_markdown(output_dir: Path, source_stem: str) -> Path | None:
    markdown_files = [path for path in output_dir.rglob("*.md") if path.is_file()]
    if not markdown_files:
        return None
    matching = [path for path in markdown_files if source_stem.lower() in path.stem.lower()]
    candidates = matching or markdown_files
    return max(candidates, key=lambda path: (path.stat().st_size, path.stat().st_mtime))


def _docx_to_text(content: bytes) -> str:
    if content[:4] != b"PK\x03\x04":
        raise RuntimeError("DOCX parser received a non-zip document")
    with (
        zipfile.ZipFile(BytesIO(content), "r") as archive,
        archive.open("word/document.xml") as document_xml,
    ):
        tree = ET.parse(document_xml)
    body = tree.find(f"{WORD_NS}body")
    if body is None:
        return ""
    blocks: list[str] = []
    for block in body:
        if block.tag == f"{WORD_NS}p":
            text = _paragraph_text(block)
            if text:
                blocks.append(text)
        elif block.tag == f"{WORD_NS}tbl":
            table = _table_to_html(block)
            if table:
                blocks.append(table)
    return "\n".join(blocks)


def _paragraph_text(paragraph: ET.Element) -> str:
    return "".join(node.text or "" for node in paragraph.iter(f"{WORD_NS}t")).strip()


def _table_to_html(table: ET.Element) -> str:
    rows: list[str] = []
    for row in table.findall(f"{WORD_NS}tr"):
        cells: list[str] = []
        for cell in row.findall(f"{WORD_NS}tc"):
            cell_text = "\n".join(
                text
                for paragraph in cell.findall(f"{WORD_NS}p")
                if (text := _paragraph_text(paragraph))
            )
            cells.append(f"<td>{escape(cell_text)}</td>")
        if cells:
            rows.append(f"<tr>{''.join(cells)}</tr>")
    return f"<table>{''.join(rows)}</table>" if rows else ""


def _walk_content(node: object, path: list[str] | None = None) -> list[str]:
    path = path or []
    if not isinstance(node, dict):
        return []
    title = str(node.get("title") or "").strip()
    next_path = path + ([title] if title else [])
    lines: list[str] = []
    children = node.get("children")
    if title:
        lines.append(" / ".join(next_path))
    if isinstance(children, list):
        for child in children:
            lines.extend(_walk_content(child, next_path))
    elif isinstance(children, str) and children.strip():
        lines.append(children.strip())
    return lines


def _json_to_text(data: object) -> str:
    if not isinstance(data, dict):
        return json.dumps(data, ensure_ascii=False)
    payload = data.get("data", data)
    if not isinstance(payload, dict):
        return json.dumps(data, ensure_ascii=False)
    lines = [
        str(payload.get("title") or "").strip(),
        f"制定机关：{payload.get('zdjgName') or ''}".strip(),
        f"公布日期：{payload.get('gbrq') or ''}".strip(),
        f"施行日期：{payload.get('sxrq') or ''}".strip(),
        f"法规类型：{payload.get('flxz') or ''}".strip(),
    ]
    lines.extend(_walk_content(payload.get("content")))
    return "\n".join(line for line in lines if line.strip())


def normalize_source(
    record: SourceRecord,
    raw_path: Path,
    parser: ParserEngine = "auto",
    parser_output_dir: Path | None = None,
) -> Document:
    parsed = _read_text(raw_path, parser=parser, parser_output_dir=parser_output_dir)
    text = parsed.text
    return Document(
        doc_id=record.source_id,
        source_id=record.source_id,
        title=record.title,
        source_url=record.source_url,
        download_url=record.download_url,
        source_site=record.source_site,
        doc_type=record.doc_type,
        authority=record.authority,
        law_status=record.law_status,
        publish_date=record.publish_date,
        effective_date=record.effective_date,
        issuing_body=record.issuing_body,
        language=record.language,
        applicable_region=record.applicable_region,
        legal_domain=record.legal_domain,
        applicable_subjects=record.applicable_subjects,
        case_no=record.case_no,
        court=record.court,
        trial_instance=record.trial_instance,
        contract_parties=record.contract_parties,
        clause_type=record.clause_type,
        topic_tags=record.topic_tags,
        raw_format=raw_path.suffix.lstrip(".") or record.file_format,
        text=text,
        ingest_meta=IngestMeta(
            fetched_at=datetime.now(UTC).isoformat(),
            parser=parsed.parser,
            parser_version=parsed.parser_version,
        ),
    )
