"""Report writing conventions and Markdown parsing shared by delivery paths."""

from __future__ import annotations

import re
from html import unescape

from markdown_it import MarkdownIt

REPORT_WRITING_GUIDANCE = (
    "围绕当前目标先给判断，再按实际内容用Markdown三级小标题组织关键事实、规则及个案适用。"
    "每段聚焦一个判断，短段落与列表结合；用**加粗**突出决定性判断、关键前提和待确认事项，"
    "不整段加粗，条件性结论须将条件与结论一起强调。确有条件或路径比较时可使用不超过三列的"
    "GFM表格，不将长篇推导塞入表格、不强制凑表。表格中的法律判断同样关联claims，"
    "claims保留对应段落、列表项或单元格的原表述；引用编号和HTML标记由程序处理。"
    "未知不写成否定，不重复摘要、判断要点或边界清单。"
)


def report_markdown() -> MarkdownIt:
    return MarkdownIt("commonmark", {"html": False}).enable("table")


def pdf_report_text(value: object) -> str:
    text = re.sub(
        r'<sup\b[^<>]*\bdata-citation-ref="[^"<>]+"[^<>]*>([^<>]+)</sup>',
        lambda match: f" [{match.group(1)}]", str(value or ""),
    )
    return unescape(re.sub(r"<[^>]+>", "", text))


_USAGE_SENTENCES = tuple(re.compile(pattern) for pattern in (
    r"本结论基于当前材料(?:和|及|、)\s*已召回证据[，,、]?\s*不构成正式法律意见[。]?",
    r"(?:本报告|本结论)(?:仅供参考[，,])?不构成正式法律意见[。]?",
    r"本报告由\s*CrossComply\s*自动生成[，,]仅供研究参考[，,]不构成正式法律意见[。]?",
    r"系统仅提供合规决策辅助[，,]不替代专业法律意见[。]?",
))


def partition_report_content(conclusion: str, boundaries: tuple[str, ...]):
    usage: list[str] = []

    def separate(value: str) -> str:
        parts = re.findall(r"[^\n。！？]*(?:[。！？](?:\*\*|__)?|\n|$)", value)
        remaining = []
        for part in parts:
            sentence = re.sub(r"^[-*+]\s+", "", part.strip()).replace("**", "").replace("__", "")
            if any(pattern.fullmatch(sentence) for pattern in _USAGE_SENTENCES):
                if sentence not in usage:
                    usage.append(sentence)
            else:
                remaining.append(part)
        return re.sub(r"\n{3,}", "\n\n", "".join(remaining)).strip()

    body = separate(conclusion)
    conditions = tuple(dict.fromkeys(filter(None, (separate(item) for item in boundaries))))
    return body, conditions, tuple(usage)
