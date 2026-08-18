"""Deterministic signed decision-report rendering."""

from __future__ import annotations

import hashlib
import io
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class LegalSource:
    title: str
    locator: str


@dataclass(frozen=True)
class DecisionReportData:
    case_number: str
    decision: str
    material_hashes: tuple[str, ...]
    rule_version: str
    legal_sources: tuple[LegalSource, ...]
    remediation_items: tuple[str, ...]
    approver: str
    approved_at: str


@dataclass(frozen=True)
class DecisionReportArtifact:
    pdf_bytes: bytes
    sha256: str


def generate_decision_report(data: DecisionReportData) -> DecisionReportArtifact:
    """Render a byte-for-byte deterministic PDF and return its SHA-256."""

    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        from reportlab.pdfgen.canvas import Canvas
    except ImportError as exc:  # pragma: no cover - exercised in minimal deployments
        raise RuntimeError(
            "生成 PDF 决策报告需要 reportlab；请在部署依赖中安装 reportlab>=4.0"
        ) from exc

    buffer = io.BytesIO()
    canvas = Canvas(
        buffer,
        pagesize=A4,
        invariant=1,
        pageCompression=0,
        pdfVersion=(1, 4),
    )
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    _, height = A4
    margin = 54
    y = height - margin
    canvas.setTitle(f"CrossComply Decision Report {data.case_number}")
    canvas.setAuthor("CrossComply")
    canvas.setCreator("CrossComply")

    def line(text: str, *, size: int = 10, leading: int = 15) -> None:
        nonlocal y
        if y < margin + leading:
            canvas.showPage()
            y = height - margin
        canvas.setFont("STSong-Light", size)
        canvas.drawString(margin, y, text)
        y -= leading

    line("CrossComply Decision Report", size=16, leading=24)
    line(f"Case number: {data.case_number}")
    line(f"Decision: {data.decision}")
    line(f"Rule version: {data.rule_version}")
    line(f"Approver: {data.approver}")
    line(f"Approval time: {data.approved_at}", leading=22)

    _render_section(line, "Material SHA-256", data.material_hashes)
    _render_section(
        line,
        "Legal sources",
        (f"{source.title} | {source.locator}" for source in data.legal_sources),
    )
    _render_section(line, "Remediation items", data.remediation_items)

    canvas.showPage()
    canvas.save()
    pdf_bytes = buffer.getvalue()
    return DecisionReportArtifact(
        pdf_bytes=pdf_bytes,
        sha256=hashlib.sha256(pdf_bytes).hexdigest(),
    )


def write_decision_report(
    data: DecisionReportData,
    destination: str | Path,
) -> DecisionReportArtifact:
    """Atomically write a report artifact to a caller-selected destination."""

    artifact = generate_decision_report(data)
    path = Path(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(artifact.pdf_bytes)
    temporary.replace(path)
    return artifact


def verify_report_hash(pdf_bytes: bytes, expected_sha256: str) -> bool:
    """Compare a downloaded report with its persisted digest."""

    actual = hashlib.sha256(pdf_bytes).hexdigest()
    return len(expected_sha256) == 64 and actual == expected_sha256.lower()


def _render_section(
    draw: Callable[..., None],
    heading: str,
    items: Iterable[str],
) -> None:
    draw(heading, size=12, leading=19)
    rendered = False
    for index, item in enumerate(items, start=1):
        rendered = True
        draw(f"{index}. {item}")
    if not rendered:
        draw("None")
    draw("")
