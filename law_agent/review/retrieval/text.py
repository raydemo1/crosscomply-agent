"""Small text helpers used by retrieval orchestration."""

from __future__ import annotations

import re
import unicodedata

_PUNCT_RE = re.compile(r"[，。；：、（）《》""''【】「」　]")
_STRIP_RE = re.compile(r"[^\u4e00-\u9fff\u3400-\u4dbfA-Za-z0-9\s]")
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_ASCII_RUN_RE = re.compile(r"[a-z0-9]+(?:[/-][a-z0-9]+)*")


def normalize_text(text: str) -> str:
    """Normalize Chinese punctuation and ASCII case."""

    text = unicodedata.normalize("NFC", text)
    text = _PUNCT_RE.sub(" ", text)
    text = _STRIP_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def tokenize(text: str) -> list[str]:
    """Tokenize text into Chinese bigrams plus ASCII/number tokens."""

    normalized = normalize_text(text)
    tokens: list[str] = []
    ascii_runs = _ASCII_RUN_RE.findall(normalized)
    tokens.extend(run.replace(" ", "") for run in ascii_runs if run.strip())

    cjk_text = _ASCII_RUN_RE.sub(" ", normalized)
    cjk_chars = [char for char in cjk_text if _CJK_RE.match(char)]
    if len(cjk_chars) <= 1:
        tokens.extend(cjk_chars)
    else:
        tokens.extend(
            cjk_chars[index] + cjk_chars[index + 1]
            for index in range(len(cjk_chars) - 1)
        )
        tokens.append(cjk_chars[-1])
    return [token for token in tokens if token]
