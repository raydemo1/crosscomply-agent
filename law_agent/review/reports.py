"""Deterministic, brand-aligned decision-report rendering."""

from __future__ import annotations

import hashlib
import html
import io
import os
import re
from dataclasses import dataclass
from pathlib import Path

try:
    from reportlab.lib.units import mm
    from reportlab.platypus import Flowable, TableStyle
except ImportError:  # pragma: no cover - allows the module to expose a useful runtime error
    mm = 2.834645669

    class Flowable:  # type: ignore[no-redef]
        pass

    TableStyle = None  # type: ignore[assignment,misc]


@dataclass(frozen=True)
class LegalSource:
    title: str
    locator: str
    article: str = ""
    provision_text: str = ""
    application: str = ""
    role: str = "legal_basis"
    citation_ref: str = ""


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
    case_title: str = ""
    selected_path: str = ""
    rule_findings: tuple[str, ...] = ()
    manual_confirmation_items: tuple[str, ...] = ()
    ai_review: AIReviewSummary | None = None
    remediation_details: tuple[RemediationDetail, ...] = ()


@dataclass(frozen=True)
class AIReviewSummary:
    risk_level: str = ""
    conclusion: str = ""
    missing_information: tuple[str, ...] = ()
    recommended_actions: tuple[str, ...] = ()
    risk_boundaries: tuple[str, ...] = ()
    business_activity: str = ""
    overseas_recipient: str = ""
    processing_purpose: str = ""
    data_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class RemediationDetail:
    title: str
    description: str = ""
    owner_role: str = ""
    priority: str = ""
    required_before: str = ""
    status: str = ""
    evidence_expected: str = ""


@dataclass(frozen=True)
class DecisionReportArtifact:
    pdf_bytes: bytes
    sha256: str


NAVY = "#1E3A8A"
DEEP_NAVY = "#142B63"
BLUE = "#1E40AF"
GOLD = "#B45309"
INK = "#0F172A"
MUTED = "#475569"
BG = "#F8FAFC"
BORDER = "#CBD5E1"
SOFT_BLUE = "#EFF6FF"
SOFT_GOLD = "#FFFBEB"
SOFT_GREEN = "#F0FDF4"
GREEN = "#047857"
RED = "#B91C1C"


def generate_decision_report(data: DecisionReportData) -> DecisionReportArtifact:
    """Render a deterministic, multi-page PDF and return its SHA-256."""

    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen.canvas import Canvas
        from reportlab.platypus import (
            BaseDocTemplate,
            Flowable,
            Frame,
            PageBreak,
            PageTemplate,
            Paragraph,
            Spacer,
            Table,
            TableStyle,
        )
    except ImportError as exc:  # pragma: no cover - exercised in minimal deployments
        raise RuntimeError(
            "生成 PDF 决策报告需要 reportlab；请在部署依赖中安装 reportlab>=4.0"
        ) from exc

    body_font, bold_font, mono_font = _register_fonts(pdfmetrics, TTFont)
    page_width, page_height = A4
    left_margin = 17 * mm
    right_margin = 17 * mm
    top_margin = 25 * mm
    bottom_margin = 16 * mm
    styles = _build_styles(
        ParagraphStyle,
        getSampleStyleSheet,
        body_font=body_font,
        bold_font=bold_font,
        mono_font=mono_font,
        colors=colors,
    )
    buffer = io.BytesIO()

    class StableCanvas(Canvas):
        def __init__(self, *args, **kwargs):
            kwargs["invariant"] = 1
            kwargs["pageCompression"] = 0
            super().__init__(*args, **kwargs)

    doc = BaseDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=left_margin,
        rightMargin=right_margin,
        topMargin=top_margin,
        bottomMargin=bottom_margin,
        title=f"CrossComply Decision Report {data.case_number}",
        author="CrossComply",
        creator="CrossComply",
        pageCompression=0,
    )
    frame = Frame(
        left_margin,
        bottom_margin,
        page_width - left_margin - right_margin,
        page_height - top_margin - bottom_margin,
        id="main",
        leftPadding=0,
        rightPadding=0,
        topPadding=0,
        bottomPadding=0,
    )
    doc.addPageTemplates([
        PageTemplate(
            id="report",
            frames=[frame],
            onPage=lambda canvas, _doc: _draw_page_chrome(
                canvas,
                canvas.getPageNumber(),
                page_width,
                page_height,
                colors,
                body_font,
                bold_font,
            ),
        )
    ])

    story: list[Flowable] = []
    story.extend(_cover_page(data, styles, colors, page_width, body_font, bold_font))
    story.append(PageBreak())
    story.extend(_rich_decision_packet(data, styles, colors))
    doc.build(story, canvasmaker=StableCanvas)

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


def _register_fonts(pdfmetrics, tt_font):
    """Load the approved Noto Sans SC regular and semibold faces without substitution."""

    regular_candidates = [
        os.getenv("CROSSCOMPLY_REPORT_FONT", ""),
        "C:/Windows/Fonts/NotoSansSC-Regular.ttf",
        str(Path(__file__).resolve().parents[2] / "docker/fonts/NotoSansSC-Regular.ttf"),
        "/usr/local/share/fonts/NotoSansSC-Regular.ttf",
    ]
    bold_candidates = [
        os.getenv("CROSSCOMPLY_REPORT_FONT_BOLD", ""),
        "C:/Windows/Fonts/NotoSansSC-SemiBold.ttf",
        str(Path(__file__).resolve().parents[2] / "docker/fonts/NotoSansSC-SemiBold.ttf"),
        "/usr/local/share/fonts/NotoSansSC-SemiBold.ttf",
    ]
    regular_path = next((path for path in regular_candidates if path and Path(path).is_file()), None)
    bold_path = next((path for path in bold_candidates if path and Path(path).is_file()), None)
    if not regular_path or not bold_path:
        raise RuntimeError(
            "生成 PDF 决策报告需要 Noto Sans SC Regular 与 SemiBold 字体；"
            "请设置 CROSSCOMPLY_REPORT_FONT 和 CROSSCOMPLY_REPORT_FONT_BOLD，或安装对应字体"
        )
    try:
        pdfmetrics.registerFont(tt_font("CCBody", regular_path))
        pdfmetrics.registerFont(tt_font("CCBodyBold", bold_path))
        pdfmetrics.registerFontFamily(
            "CCBody",
            normal="CCBody",
            bold="CCBodyBold",
            italic="CCBody",
            boldItalic="CCBodyBold",
        )
    except Exception as exc:  # pragma: no cover - depends on host font support
        raise RuntimeError(
            "无法加载 Noto Sans SC Regular/SemiBold 字体，请检查字体路径配置"
        ) from exc
    return "CCBody", "CCBodyBold", "Courier"


def _build_styles(ParagraphStyle, get_sample_styles, *, body_font, bold_font, mono_font, colors):
    sample = get_sample_styles()
    return {
        "body": ParagraphStyle("body", parent=sample["BodyText"], fontName=body_font, fontSize=10, leading=15, textColor=colors.HexColor(INK), spaceAfter=5),
        "body_muted": ParagraphStyle("body_muted", parent=sample["BodyText"], fontName=body_font, fontSize=9.2, leading=13, textColor=colors.HexColor(MUTED)),
        "cover_kicker": ParagraphStyle("cover_kicker", parent=sample["BodyText"], fontName=bold_font, fontSize=10, leading=13, textColor=colors.HexColor(GOLD), tracking=1.2),
        "cover_title": ParagraphStyle("cover_title", parent=sample["Title"], fontName=bold_font, fontSize=34, leading=43, textColor=colors.HexColor(INK), spaceAfter=10),
        "cover_case": ParagraphStyle("cover_case", parent=sample["BodyText"], fontName=mono_font, fontSize=10, leading=15, textColor=colors.HexColor(MUTED)),
        "section": ParagraphStyle("section", parent=sample["Heading2"], fontName=bold_font, fontSize=18, leading=24, textColor=colors.HexColor(INK), spaceBefore=1, spaceAfter=8),
        "section_note": ParagraphStyle("section_note", parent=sample["BodyText"], fontName=body_font, fontSize=9.2, leading=13, textColor=colors.HexColor(MUTED), spaceAfter=10),
        "lead": ParagraphStyle("lead", parent=sample["BodyText"], fontName=body_font, fontSize=10.5, leading=16, textColor=colors.HexColor(INK), spaceAfter=8),
        "source_title": ParagraphStyle("source_title", parent=sample["Heading3"], fontName=bold_font, fontSize=12.5, leading=18, textColor=colors.HexColor(NAVY), spaceBefore=5, spaceAfter=5),
        "source_group": ParagraphStyle("source_group", parent=sample["Heading3"], fontName=bold_font, fontSize=13.2, leading=18, textColor=colors.HexColor(NAVY), spaceBefore=8, spaceAfter=5),
        "source_quote": ParagraphStyle("source_quote", parent=sample["BodyText"], fontName=body_font, fontSize=9.4, leading=14, textColor=colors.HexColor(MUTED), leftIndent=10, rightIndent=4, spaceAfter=6),
        "card_label": ParagraphStyle("card_label", parent=sample["BodyText"], fontName=bold_font, fontSize=8, leading=11, textColor=colors.HexColor(MUTED)),
        "card_value": ParagraphStyle("card_value", parent=sample["BodyText"], fontName=bold_font, fontSize=14, leading=19, textColor=colors.HexColor(NAVY)),
        "table_head": ParagraphStyle("table_head", parent=sample["BodyText"], fontName=bold_font, fontSize=8.2, leading=11, textColor=colors.white),
        "table_body": ParagraphStyle("table_body", parent=sample["BodyText"], fontName=body_font, fontSize=9.2, leading=13, textColor=colors.HexColor(INK)),
        "table_body_muted": ParagraphStyle("table_body_muted", parent=sample["BodyText"], fontName=body_font, fontSize=8.8, leading=12, textColor=colors.HexColor(MUTED)),
        "table_mono": ParagraphStyle("table_mono", parent=sample["BodyText"], fontName=mono_font, fontSize=8.2, leading=11, textColor=colors.HexColor(INK), splitLongWords=True),
        "callout": ParagraphStyle("callout", parent=sample["BodyText"], fontName=body_font, fontSize=9.2, leading=14, textColor=colors.HexColor(INK)),
        "opinion_label": ParagraphStyle("opinion_label", parent=sample["BodyText"], fontName=bold_font, fontSize=10.2, leading=14, textColor=colors.white),
        "opinion": ParagraphStyle("opinion", parent=sample["BodyText"], fontName=body_font, fontSize=10.8, leading=17, textColor=colors.HexColor(INK)),
    }


def _p(value: object, style, *, escape: bool = True):
    from reportlab.platypus import Paragraph

    text = "" if value is None else str(value)
    if escape:
        text = html.escape(text).replace("\n", "<br/>")
    return Paragraph(text, style)


def _section_heading(title: str, index: str, styles):
    return _p(f'<font color="{GOLD}">{index}</font>  {html.escape(title)}', styles["section"], escape=False)


def _clean_ai_text(value: object, *, limit: int = 360) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}…"


def _narrative_text(value: object, *, limit: int = 1200) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"(?m)^\s*#{1,6}\s+[^\n]{1,48}\s*$", "", text)
    text = re.sub(r"(?m)^\s*引用说明[：:].*$", "", text)
    text = re.sub(r"[①②③④⑤⑥⑦⑧⑨⑩]+", "", text)
    text = re.sub(r"(?m)^\s*[-*]\s+", "", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text).strip()
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}…"


def _narrative_html(value: object, *, limit: int = 1200) -> str:
    return html.escape(_narrative_text(value, limit=limit)).replace("\n", "<br/>")


def _review_conclusion_parts(value: object) -> tuple[str, tuple[str, ...]]:
    """Separate the readable conclusion from embedded confirmation bullets."""

    text = _narrative_text(value, limit=2200)
    lead: list[str] = []
    attention: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip().strip("；;")
        if not line or line.startswith("本结论基于"):
            continue
        if re.match(r"^(需确认|需取得|需开展|需与|材料中)", line):
            attention.append(line)
            continue
        if re.match(r"^\d+[.、]", line):
            continue
        lead.append(line)
    return "\n".join(lead[:2]), tuple(dict.fromkeys(attention))


def _strip_article_prefix(value: object, article: str) -> str:
    """Avoid repeating the article locator in both heading and quoted text."""

    text = _clean_ai_text(value, limit=760)
    if article:
        text = re.sub(rf"^\s*{re.escape(article)}\s*", "", text)
    return text.strip()


def _display_datetime(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"\.\d+(?=[+-]\d{2}:\d{2}|Z$)", "", text)
    return text.replace("T", " ")


def _risk_label(risk_level: str) -> tuple[str, str, str]:
    labels = {
        "high": ("高风险", RED, "#FEF2F2"),
        "medium": ("中风险", GOLD, SOFT_GOLD),
        "low": ("低风险", GREEN, SOFT_GREEN),
        "insufficient_evidence": ("证据不足", MUTED, "#F1F5F9"),
    }
    return labels.get(str(risk_level).strip().lower(), ("待核验", MUTED, "#F1F5F9"))


def _status_label(decision: str) -> tuple[str, str, str]:
    normalized = decision.strip().lower()
    if normalized in {"approved", "通过"}:
        return "审批通过", GREEN, SOFT_GREEN
    if normalized in {"conditionally_approved", "附条件通过"}:
        return "附条件通过", GOLD, SOFT_GOLD
    if normalized in {"rejected", "驳回"}:
        return "审批驳回", RED, "#FEF2F2"
    if normalized in {"canceled", "cancelled", "已撤回"}:
        return "审批已撤回", MUTED, "#F1F5F9"
    return decision or "待定", MUTED, "#F1F5F9"


def _cover_page(data, styles, colors, page_width, body_font, bold_font):
    from reportlab.platypus import Spacer, Table, TableStyle

    status, status_color, status_bg = _status_label(data.decision)
    source_names = "、".join(dict.fromkeys(source.title for source in data.legal_sources)) or "未记录法源"
    detail_count = len(data.remediation_details)
    action_count = max(len(data.remediation_items), detail_count)
    recommendation_count = len(data.ai_review.recommended_actions) if data.ai_review else 0
    if action_count:
        remediation = f"{action_count} 项已登记整改"
    elif recommendation_count:
        remediation = f"{recommendation_count} 项建议整改"
    else:
        remediation = "无待办整改事项"
    story: list[Flowable] = [
        Spacer(1, 10 * mm),
        BrandMark(page_width=page_width, colors=colors, body_font=body_font, bold_font=bold_font),
        Spacer(1, 15 * mm),
        _p("CROSSCOMPLY  /  DECISION REPORT", styles["cover_kicker"], escape=False),
        Spacer(1, 4 * mm),
        _p("跨境数据合规<br/>决策报告", styles["cover_title"], escape=False),
        _p(f"案件编号  {html.escape(data.case_number)}", styles["cover_case"], escape=False),
        Spacer(1, 12 * mm),
    ]
    summary = Table([
        [_p("最终决定", styles["card_label"]), _p("审批人", styles["card_label"]), _p("决定时间", styles["card_label"])],
        [_p(status, styles["card_value"]), _p(data.approver, styles["table_body"]), _p(_display_datetime(data.approved_at), styles["table_mono"])],
    ], colWidths=[48 * mm, 45 * mm, 62 * mm], rowHeights=[9 * mm, 19 * mm])
    summary.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BACKGROUND", (0, 1), (0, 1), colors.HexColor(status_bg)),
        ("TEXTCOLOR", (0, 1), (0, 1), colors.HexColor(status_color)),
        ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor(BORDER)),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor(BORDER)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 11),
        ("RIGHTPADDING", (0, 0), (-1, -1), 11),
    ]))
    story.extend([summary, Spacer(1, 15 * mm), _p("审查范围", styles["section"])])
    scope_rows = []
    if data.case_title:
        scope_rows.append([_p("审查对象", styles["table_head"]), _p(data.case_title, styles["table_body"])])
    if data.selected_path:
        scope_rows.append([_p("判定路径", styles["table_head"]), _p(data.selected_path, styles["table_body"])])
    scope_rows.extend([
        [_p("适用规则", styles["table_head"]), _p(data.rule_version, styles["table_mono"])],
        [_p("主要法源", styles["table_head"]), _p(source_names, styles["table_body"])],
        [_p("材料状态", styles["table_head"]), _p(f"{len(data.material_hashes)} 份材料已冻结并纳入本次审查", styles["table_body"])],
        [_p("整改状态", styles["table_head"]), _p(remediation, styles["table_body"])],
    ])
    scope = Table(scope_rows, colWidths=[34 * mm, 121 * mm])
    scope.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor(NAVY)),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.white),
        ("ROWBACKGROUNDS", (1, 0), (1, -1), [colors.white, colors.HexColor(BG)]),
        ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor(BORDER)),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor(BORDER)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 11),
        ("RIGHTPADDING", (0, 0), (-1, -1), 11),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))
    story.append(scope)
    return story


class BrandMark(Flowable):
    def __init__(self, *, page_width, colors, body_font, bold_font):
        super().__init__()
        self.width = page_width
        self.height = 14 *  mm
        self.colors = colors
        self.body_font = body_font
        self.bold_font = bold_font

    def draw(self):
        canvas = self.canv
        canvas.saveState()
        y = self.height - 10
        canvas.setFillColor(self.colors.HexColor(NAVY))
        canvas.rect(0, y, 18, 9, fill=1, stroke=0)
        canvas.setFillColor(self.colors.HexColor(GOLD))
        canvas.rect(8, y - 4, 18, 9, fill=1, stroke=0)
        canvas.setFillColor(self.colors.HexColor(INK))
        canvas.setFont(self.bold_font, 15)
        canvas.drawString(34, y - 1, "CrossComply")
        canvas.setFillColor(self.colors.HexColor(MUTED))
        canvas.setFont(self.body_font, 8.4)
        canvas.drawString(35, y - 13, "让合规判断有据可依")
        canvas.restoreState()


class DecisionFlow(Flowable):
    def __init__(self, *, colors, body_font, bold_font):
        super().__init__()
        self.width = 155 * mm
        self.height = 28 * mm
        self.colors = colors
        self.body_font = body_font
        self.bold_font = bold_font

    def draw(self):
        canvas = self.canv
        canvas.saveState()
        labels = [("01", "材料", "冻结快照"), ("02", "规则", "路径判定"), ("03", "证据", "深度审查"), ("04", "审批", "企业决定"), ("05", "归档", "报告留痕")]
        step_x = self.width / 5
        center_y = self.height - 12
        for i, (number, title, caption) in enumerate(labels):
            x = 14 + i * step_x
            if i < len(labels) - 1:
                canvas.setStrokeColor(self.colors.HexColor(BORDER))
                canvas.setLineWidth(1)
                canvas.line(x + 12, center_y, x + step_x - 12, center_y)
            canvas.setFillColor(self.colors.HexColor(NAVY if i < 4 else GOLD))
            canvas.circle(x, center_y, 11, fill=1, stroke=0)
            canvas.setFillColor(self.colors.white)
            canvas.setFont(self.bold_font, 7.2)
            canvas.drawCentredString(x, center_y - 2.2, number)
            canvas.setFillColor(self.colors.HexColor(INK))
            canvas.setFont(self.bold_font, 9.2)
            canvas.drawCentredString(x, center_y - 24, title)
            canvas.setFillColor(self.colors.HexColor(MUTED))
            canvas.setFont(self.body_font, 8.2)
            canvas.drawCentredString(x, center_y - 34, caption)
        canvas.restoreState()


def _decision_packet(data, styles, colors):
    from reportlab.platypus import Spacer, Table, TableStyle

    status, status_color, status_bg = _status_label(data.decision)
    story: list[Flowable] = [_section_heading("结论与依据", "01", styles)]

    decision = Table([
        [_p("审查结论", styles["card_label"]), _p("审批人", styles["card_label"]), _p("决定时间", styles["card_label"])],
        [_p(status, styles["card_value"]), _p(data.approver, styles["table_body"]), _p(data.approved_at, styles["table_mono"])],
    ], colWidths=[50 * mm, 48 * mm, 57 * mm], rowHeights=[9 * mm, 19 * mm])
    decision.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BACKGROUND", (0, 1), (0, 1), colors.HexColor(status_bg)),
        ("TEXTCOLOR", (0, 1), (0, 1), colors.HexColor(status_color)),
        ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor(BORDER)),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor(BORDER)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 11),
        ("RIGHTPADDING", (0, 0), (-1, -1), 11),
    ]))
    story.extend([decision, Spacer(1, 13 * mm), _p("适用规则与法源", styles["section"])])

    source_rows = [[_p("项目", styles["table_head"]), _p("内容", styles["table_head"])], [_p("规则版本", styles["table_body"]), _p(data.rule_version, styles["table_mono"])]]
    for source in data.legal_sources:
        source_rows.append([_p("法源", styles["table_body"]), _p(f"{source.title}<br/>{source.locator}", styles["table_body"])])
    if not data.legal_sources:
        source_rows.append([_p("法源", styles["table_body_muted"]), _p("本次报告未记录法源", styles["table_body_muted"])])
    source_table = Table(source_rows, colWidths=[32 * mm, 123 * mm], repeatRows=1)
    source_table.setStyle(_table_style(colors, header=BLUE))
    story.extend([source_table, Spacer(1, 13 * mm), _p("整改事项", styles["section"])])

    if data.remediation_items:
        action_rows = [[_p("序号", styles["table_head"]), _p("待办事项", styles["table_head"]), _p("状态", styles["table_head"])]]
        action_rows.extend([[_p(f"{i:02d}", styles["table_body"]), _p(item, styles["table_body"]), _p("待闭环", styles["table_body"])] for i, item in enumerate(data.remediation_items, 1)])
        actions = Table(action_rows, colWidths=[18 * mm, 109 * mm, 28 * mm], repeatRows=1)
        actions.setStyle(_table_style(colors, header=GOLD))
        story.append(actions)
    else:
        clear = Table([[_p("本次审查无待办整改事项", styles["card_value"])]], colWidths=[155 * mm])
        clear.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(SOFT_GREEN)),
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor(GREEN)),
            ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#BBF7D0")),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 11),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
        ]))
        story.append(clear)

    story.extend([Spacer(1, 13 * mm), _p("报告范围", styles["section"])])
    scope_note = Table([[_p(f"本报告对应案件 {data.case_number}，基于 {len(data.material_hashes)} 份已冻结材料生成。底层材料校验信息由系统留存，不在面向审批人的报告中展开。", styles["callout"])]], colWidths=[155 * mm])
    scope_note.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(SOFT_BLUE)),
        ("LINEBEFORE", (0, 0), (0, -1), 3, colors.HexColor(NAVY)),
        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#BFDBFE")),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
    ]))
    story.append(scope_note)
    return story


def _rich_decision_packet(data, styles, colors):
    from reportlab.platypus import Spacer, Table, TableStyle

    status, status_color, status_bg = _status_label(data.decision)
    story: list[Flowable] = [_section_heading("审查结论与合规依据", "01", styles)]

    if data.ai_review is not None:
        ai = data.ai_review
        risk_label, risk_color, risk_bg = _risk_label(ai.risk_level)
        conclusion_text, conclusion_attention = _review_conclusion_parts(ai.conclusion)
        risk_card = Table([
            [_p("风险等级", styles["card_label"]), _p(risk_label, styles["card_value"])],
        ], colWidths=[38 * mm, 117 * mm])
        risk_card.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.white),
            ("BACKGROUND", (0, 0), (0, 0), colors.HexColor(risk_bg)),
            ("TEXTCOLOR", (1, 0), (1, 0), colors.HexColor(risk_color)),
            ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor(BORDER)),
            ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor(BORDER)),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 11),
            ("RIGHTPADDING", (0, 0), (-1, -1), 11),
            ("TOPPADDING", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ]))
        story.append(risk_card)
        if conclusion_text:
            story.extend([
                Spacer(1, 9 * mm),
            ])
            opinion_card = Table([
                [_p("审查意见", styles["opinion_label"])],
                [_p(_narrative_html(conclusion_text, limit=900), styles["opinion"], escape=False)],
            ], colWidths=[155 * mm])
            opinion_card.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(NAVY)),
                ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor(SOFT_BLUE)),
                ("LINEBEFORE", (0, 1), (0, 1), 4, colors.HexColor(GOLD)),
                ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#BFDBFE")),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, 0), 8),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
                ("TOPPADDING", (0, 1), (-1, 1), 11),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 11),
            ]))
            story.append(opinion_card)

        context_rows = []
        for label, value in (
            ("业务场景", ai.business_activity),
            ("境外接收方", ai.overseas_recipient),
            ("处理目的", ai.processing_purpose),
            ("涉及数据", "、".join(ai.data_types)),
        ):
            if value:
                context_rows.append([_p(label, styles["table_head"]), _p(value, styles["table_body"])])
        if context_rows:
            story.extend([Spacer(1, 9 * mm), _p("关键事实", styles["section"])])
            context_table = Table(context_rows, colWidths=[30 * mm, 125 * mm])
            context_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor(NAVY)),
                ("TEXTCOLOR", (0, 0), (0, -1), colors.white),
                ("ROWBACKGROUNDS", (1, 0), (1, -1), [colors.white, colors.HexColor(BG)]),
                ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor(BORDER)),
                ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor(BORDER)),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 7),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ]))
            story.append(context_table)

        attention = list(conclusion_attention)
        attention.extend(
            f"材料缺口：{_clean_ai_text(item, limit=180)}" for item in ai.missing_information
        )
        attention.extend(f"边界：{_clean_ai_text(item, limit=180)}" for item in ai.risk_boundaries)
        attention = list(dict.fromkeys(attention))
        if attention:
            story.extend([Spacer(1, 9 * mm), _p("待核实事项", styles["section"])])
            attention_html = "<br/>".join(
                f'<font color="{RED}"><b>{index:02d}</b></font>　{html.escape(item)}'
                for index, item in enumerate(attention, 1)
            )
            attention_table = Table([[_p(attention_html, styles["callout"], escape=False)]], colWidths=[155 * mm])
            attention_table.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(SOFT_GOLD)),
                ("LINEBEFORE", (0, 0), (0, -1), 3, colors.HexColor(GOLD)),
                ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#FDE68A")),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
            ]))
            story.append(attention_table)

        if ai.recommended_actions:
            story.extend([
                Spacer(1, 9 * mm),
                _p("建议整改", styles["section"]),
                _p("结合本次审查结论，建议按以下顺序推进：", styles["section_note"]),
            ])
            for index, item in enumerate(ai.recommended_actions, 1):
                recommendation = html.escape(_narrative_text(item, limit=300))
                story.append(
                    _p(f'<font color="{GOLD}">{index:02d}</font>　{recommendation}', styles["body"], escape=False)
                )

    story.extend([Spacer(1, 11 * mm), _p("适用法律与本案分析", styles["section"])])
    if data.selected_path:
        story.append(_p(f"本案当前适用路径为“{_clean_ai_text(data.selected_path, limit=180)}”。", styles["lead"]))
    if data.legal_sources:
        grouped_sources: list[tuple[str, list[LegalSource]]] = []
        for source in data.legal_sources:
            if grouped_sources and grouped_sources[-1][0] == source.title:
                grouped_sources[-1][1].append(source)
            else:
                grouped_sources.append((source.title, [source]))
        story.extend([Spacer(1, 3 * mm), _p("引用法源", styles["source_group"])])
        source_index_rows = [[_p("法源", styles["table_head"]), _p("法律 / 规章", styles["table_head"]), _p("本次引用条文", styles["table_head"])]]
        for group_index, (title, sources) in enumerate(grouped_sources, 1):
            articles = "、".join(source.article for source in sources if source.article) or "具体条款定位"
            source_index_rows.append([
                _p(f"法源 {group_index:02d}", styles["table_body"]),
                _p(title, styles["table_body"]),
                _p(articles, styles["table_body_muted"]),
            ])
        source_index = Table(source_index_rows, colWidths=[25 * mm, 78 * mm, 52 * mm], repeatRows=1)
        source_index.setStyle(_table_style(colors, header=BLUE))
        story.append(source_index)
        for group_index, (title, sources) in enumerate(grouped_sources, 1):
            story.append(_p(f'<font color="{GOLD}"><b>法源 {group_index:02d}</b></font>　《{html.escape(_clean_ai_text(title, limit=120))}》', styles["source_group"], escape=False))
            for article_index, source in enumerate(sources, 1):
                article = source.article or (
                    source.locator
                    if source.locator and not source.locator.lower().startswith(("http://", "https://"))
                    else "具体条款"
                )
                heading = f"{article_index:02d}　{_clean_ai_text(article, limit=80)}"
                story.append(_p(html.escape(heading), styles["source_title"], escape=False))
                if source.provision_text:
                    provision = html.escape(_strip_article_prefix(source.provision_text, source.article)).replace("\n", "<br/>")
                    story.append(_p(f"法条原文：{provision}", styles["source_quote"], escape=False))
                else:
                    story.append(_p("当前记录未保存可直接引用的条文全文。", styles["source_quote"]))
                if source.application:
                    story.append(_p(f"本案适用：{_clean_ai_text(source.application, limit=460)}", styles["body"]))
    else:
        story.append(_p("当前结果未保存可直接引用的具体条文，因此报告不把内部规则编号当作法律依据；请先补齐法源引用后再形成正式报告。", styles["body_muted"]))
    if data.manual_confirmation_items:
        story.extend([Spacer(1, 9 * mm), _p("人工确认点", styles["section"])])
        manual_text = "<br/>".join(f"{index}. {html.escape(_clean_ai_text(item, limit=220))}" for index, item in enumerate(data.manual_confirmation_items, 1))
        manual = Table([[_p(manual_text, styles["callout"], escape=False)]], colWidths=[155 * mm])
        manual.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(SOFT_BLUE)),
            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#BFDBFE")),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 9),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ]))
        story.append(manual)

    story.extend([Spacer(1, 11 * mm), _p("审批记录", styles["section"])])
    approval_rows = [
        [_p("案件编号", styles["table_head"]), _p(data.case_number, styles["table_mono"])],
        [_p("企业决定", styles["table_head"]), _p(status, styles["card_value"])],
        [_p("审批人", styles["table_head"]), _p(data.approver, styles["table_body"])],
        [_p("决定时间", styles["table_head"]), _p(_display_datetime(data.approved_at), styles["table_mono"])],
    ]
    approval = Table(approval_rows, colWidths=[32 * mm, 123 * mm])
    approval.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor(NAVY)),
        ("TEXTCOLOR", (0, 0), (0, -1), colors.white),
        ("ROWBACKGROUNDS", (1, 0), (1, -1), [colors.white, colors.HexColor(BG)]),
        ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor(BORDER)),
        ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor(BORDER)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    story.append(approval)

    details = list(data.remediation_details)
    if not details and data.remediation_items:
        details = [RemediationDetail(title=item) for item in data.remediation_items]
    if details:
        story.extend([Spacer(1, 11 * mm), _p("整改计划", styles["section"])])
        has_evidence = any(item.evidence_expected.strip() for item in details)
        if has_evidence:
            action_rows = [[_p("整改事项", styles["table_head"]), _p("责任角色", styles["table_head"]), _p("完成节点", styles["table_head"]), _p("验收证据", styles["table_head"])]]
            action_widths = [55 * mm, 35 * mm, 23 * mm, 42 * mm]
        else:
            action_rows = [[_p("整改事项", styles["table_head"]), _p("责任角色", styles["table_head"]), _p("完成节点", styles["table_head"])]]
            action_widths = [76 * mm, 43 * mm, 36 * mm]
        for item in details:
            title = html.escape(_clean_ai_text(item.title, limit=180))
            if item.description and item.description.strip() != item.title.strip():
                title += f"<br/><font color=\"{MUTED}\">{html.escape(_clean_ai_text(item.description, limit=180))}</font>"
            meta = " · ".join(value for value in (item.priority, item.status) if value)
            if meta:
                title += f"<br/><font color=\"{GOLD}\">{html.escape(meta)}</font>"
            row = [
                _p(title, styles["table_body"], escape=False),
                _p(item.owner_role or "待指定", styles["table_body"]),
                _p(item.required_before or "待确认", styles["table_body"]),
            ]
            if has_evidence:
                row.append(_p(item.evidence_expected, styles["table_body"]))
            action_rows.append(row)
        action_table = Table(action_rows, colWidths=action_widths, repeatRows=1)
        action_table.setStyle(_table_style(colors, header=GOLD))
        story.append(action_table)
    elif data.remediation_items:
        story.extend([Spacer(1, 11 * mm), _p("整改计划", styles["section"])])
        story.append(_p("本次审查未形成带责任人的整改动作，请在审批前补充整改任务。", styles["body_muted"]))
    elif data.ai_review and data.ai_review.recommended_actions:
        # Recommendations are already visible above.  Do not add a standalone
        # one-line section that can be pushed onto an otherwise blank page.
        pass
    else:
        story.extend([Spacer(1, 11 * mm), _p("整改计划", styles["section"])])
        clear = Table([[_p("本次审查无待办整改事项", styles["card_value"])]], colWidths=[155 * mm])
        clear.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(SOFT_GREEN)),
            ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor(GREEN)),
            ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#BBF7D0")),
            ("LEFTPADDING", (0, 0), (-1, -1), 12),
            ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ("TOPPADDING", (0, 0), (-1, -1), 11),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
        ]))
        story.append(clear)
    return story


def _table_style(colors, *, header: str):
    return TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(header)),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor(BG)]),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor(BORDER)),
        ("INNERGRID", (0, 0), (-1, -1), 0.35, colors.HexColor(BORDER)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ])


def _draw_page_chrome(canvas, page_num, page_width, page_height, colors, body_font, bold_font):
    canvas.saveState()
    canvas.setFillColor(colors.HexColor(BG))
    canvas.rect(0, 0, page_width, page_height, fill=1, stroke=0)
    if page_num == 1:
        canvas.setFillColor(colors.HexColor(DEEP_NAVY))
        canvas.rect(0, page_height - 6 * mm, page_width, 6 * mm, fill=1, stroke=0)
        canvas.setFillColor(colors.HexColor(GOLD))
        canvas.rect(page_width - 34 * mm, page_height - 6 * mm, 34 * mm, 6 * mm, fill=1, stroke=0)
    else:
        canvas.setFillColor(colors.HexColor(DEEP_NAVY))
        canvas.rect(0, page_height - 3 * mm, page_width, 3 * mm, fill=1, stroke=0)
        canvas.setFillColor(colors.HexColor(NAVY))
        canvas.setFont(bold_font, 7.5)
        canvas.drawString(17 * mm, page_height - 13 * mm, "CrossComply")
        canvas.setFillColor(colors.HexColor(MUTED))
        canvas.setFont(body_font, 7)
        canvas.drawRightString(page_width - 17 * mm, page_height - 13 * mm, "决策报告  /  审计归档")
    canvas.setStrokeColor(colors.HexColor(BORDER))
    canvas.setLineWidth(0.45)
    canvas.line(17 * mm, 11 * mm, page_width - 17 * mm, 11 * mm)
    canvas.setFillColor(colors.HexColor(MUTED))
    canvas.setFont(body_font, 6.8)
    canvas.drawString(17 * mm, 7 * mm, "CrossComply  ·  受控决策辅助")
    canvas.drawRightString(page_width - 17 * mm, 7 * mm, f"{page_num:02d}")
    canvas.restoreState()
