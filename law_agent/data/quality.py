"""Deterministic parse-quality evaluation for ingested source text.

The ingestion gate has to answer one question without a model: did the parser
return the document, or did it return damage?  This module detects the
artifacts a parser *introduces* — character spacing that splits numbers and
acronyms, decode corruption, leftover contents-leader runs and character-soup
output.  It never rewrites text, never calls a model and never inspects legal
meaning: callers decide what to do with the verdict.

The same verdict drives two decisions:

* parser routing, by comparing a structured re-parse against the embedded text
  layer (:func:`is_degraded`);
* the ingestion gate, by refusing to publish a body that is still visibly
  corrupted once every candidate parser has been tried.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

QualityStatus = Literal["ok", "warn", "fail"]

# Below this many characters a parsed body cannot carry a legal claim, so the
# parse counts as failed rather than merely thin.
MIN_MEANINGFUL_CHARS = 100
# A structured re-parse that keeps less of the document than this fraction of
# the embedded text layer dropped text instead of reformatting it.
MIN_COVERAGE_RATIO = 0.6
# Single-character tokens above this share of all tokens means character soup.
# Kept as a warning rather than a failure: a centered Chinese title is
# letter-spaced by the publisher's own text layer ("中 华 人 民 共 和 国"), which
# lands in the same ratio band as genuine soup, so the ratio cannot decide
# alone. The unambiguous classes below still carry the failure verdict.
SINGLE_CHAR_TOKEN_RATIO = 0.30
MIN_TOKENS_FOR_RATIO = 40
# Occurrences before an unambiguous artifact class fails the parse instead of
# merely warning about it.  One or two stray hits can be real document content
# (a table column of digits, an English slash pair); a run of them cannot.
DEFAULT_FAIL_AT = 3

# ``GB / T 4 3 6 9 7 - 2 0 2 4`` / ``2 0 2 1 年``: three or more space-separated
# single digits.  Prose never spaces out every digit of a number.
SPACED_DIGITS_RE = re.compile(r"\d(?:[ \u3000]\d){2,}")
# ``S A C``: an acronym emitted one letter per token.
SPACED_LATIN_RE = re.compile(r"(?<![A-Za-z])[A-Za-z](?:[ \u3000][A-Za-z]){2,}(?![A-Za-z])")
# ``T C2 6``: a Latin/digit token whose trailing digits were split off.
MIXED_FRAGMENT_RE = re.compile(r"[A-Za-z]{1,3}\d(?:[ \u3000]\d)+")
# ``GB / T`` / ``SAC / TC``: an identifier split around its slash.  The clean
# form ``GB/T`` carries no space and is deliberately not matched.
BROKEN_IDENTIFIER_RE = re.compile(r"[A-Z]{2,}[ \u3000]+/|/[ \u3000]+[A-Z]{2,}")
# U+FFFD plus the C0 bytes a text-layer or OCR decode can leave behind.
CORRUPTION_RE = re.compile(r"[\ufffd\u0000-\u0008\u000b\u000c\u000e-\u001f]")
# Four or more leader characters: a contents line whose page number was lost.
TOC_LEADER_RE = re.compile(r"[.·…．]{4,}")


@dataclass(frozen=True)
class _Detector:
    code: str
    pattern: re.Pattern[str]
    # ``None`` marks a detector that never fails a parse: what it finds is real
    # document content rather than proof of a damaged parse.
    fail_at: int | None = DEFAULT_FAIL_AT


# Order matters only for the reported issue list; every detector always runs.
_DETECTORS = (
    _Detector("corruption_chars", CORRUPTION_RE, fail_at=1),
    _Detector("spaced_digits", SPACED_DIGITS_RE),
    _Detector("spaced_latin", SPACED_LATIN_RE),
    _Detector("mixed_fragment", MIXED_FRAGMENT_RE),
    _Detector("broken_identifier", BROKEN_IDENTIFIER_RE),
    # A standard's own contents page carries leader runs, so their presence is
    # not damage. The cleaner removes the contents block; whatever survives is
    # reported here as a warning and is measured as the TOC-artifact count.
    _Detector("toc_leader", TOC_LEADER_RE, fail_at=None),
)


@dataclass(frozen=True)
class QualityIssue:
    """One artifact class detected in a candidate parse."""

    code: str
    severity: QualityStatus
    count: int
    sample: str = ""


@dataclass(frozen=True)
class QualityResult:
    """Verdict for one candidate text plus the artifacts behind it."""

    status: QualityStatus
    char_count: int
    issues: list[QualityIssue] = field(default_factory=list)

    @property
    def codes(self) -> list[str]:
        return sorted({issue.code for issue in self.issues})

    @property
    def damage(self) -> int:
        """Artifact total, with failures weighted above warnings."""
        return sum(issue.count * (10 if issue.severity == "fail" else 1) for issue in self.issues)

    def counts(self) -> dict[str, int]:
        return {issue.code: issue.count for issue in self.issues}

    def describe(self) -> str:
        """One-line diagnostic for logs and ingestion errors."""
        if not self.issues:
            return f"quality={self.status} chars={self.char_count}"
        detail = ", ".join(f"{issue.code}={issue.count}" for issue in self.issues)
        return f"quality={self.status} chars={self.char_count} ({detail})"

    def provenance(self) -> dict[str, object]:
        """Compact, serializable summary for ingest metadata."""
        return {"status": self.status, "issues": self.codes, "counts": self.counts()}


def _scan(detector: _Detector, text: str) -> QualityIssue | None:
    matches = [match.group(0) for match in detector.pattern.finditer(text)]
    if not matches:
        return None
    fails = detector.fail_at is not None and len(matches) >= detector.fail_at
    return QualityIssue(
        code=detector.code,
        severity="fail" if fails else "warn",
        count=len(matches),
        sample=matches[0].strip()[:60],
    )


def _single_char_issue(text: str) -> QualityIssue | None:
    """Detect character-soup output: most tokens are a single character."""

    tokens = text.split()
    if len(tokens) < MIN_TOKENS_FOR_RATIO:
        return None
    singles = sum(1 for token in tokens if len(token) == 1)
    ratio = singles / len(tokens)
    if ratio <= SINGLE_CHAR_TOKEN_RATIO:
        return None
    return QualityIssue(
        code="single_char_tokens",
        severity="warn",
        count=singles,
        sample=f"{ratio:.0%} of {len(tokens)} tokens",
    )


def _status_for(issues: list[QualityIssue]) -> QualityStatus:
    if any(issue.severity == "fail" for issue in issues):
        return "fail"
    return "warn" if issues else "ok"


def evaluate_text(text: str, *, min_chars: int = MIN_MEANINGFUL_CHARS) -> QualityResult:
    """Grade one candidate parse. Deterministic; no model and no rewriting."""

    stripped = text.strip()
    char_count = len(stripped)
    if char_count < min_chars:
        return QualityResult(
            status="fail",
            char_count=char_count,
            issues=[QualityIssue("empty_extraction", "fail", 1, stripped[:40])],
        )

    issues = [issue for detector in _DETECTORS if (issue := _scan(detector, stripped))]
    single_char_issue = _single_char_issue(stripped)
    if single_char_issue is not None:
        issues.append(single_char_issue)

    return QualityResult(status=_status_for(issues), char_count=char_count, issues=issues)


def evaluate_chunks(texts: Iterable[str]) -> QualityResult:
    """Grade each published chunk body and aggregate the artifacts found.

    Chunks are graded individually rather than as one concatenation: damage
    confined to a single clause still has to block publication, and a clean
    majority must not mask it. ``min_chars`` is 1 because a legitimate short
    article can be a dozen characters long, so only real artifacts — never
    brevity — can fail this gate.
    """

    issues: list[QualityIssue] = []
    char_count = 0
    for text in texts:
        result = evaluate_text(text, min_chars=1)
        char_count += result.char_count
        issues.extend(issue for issue in result.issues if issue.code != "empty_extraction")

    return QualityResult(status=_status_for(issues), char_count=char_count, issues=issues)


def is_degraded(candidate: QualityResult, baseline: QualityResult) -> bool:
    """Whether a structured re-parse is worse than the embedded text layer.

    Coverage is checked first so a parser that silently dropped text is
    rejected even when the surviving fragment is spotless, then artifact
    damage so a parser that introduced new artifacts is rejected even when
    both verdicts read ``ok`` or ``warn``.
    """

    if baseline.char_count == 0:
        return False
    if candidate.char_count < baseline.char_count * MIN_COVERAGE_RATIO:
        return True
    return candidate.damage > baseline.damage


__all__ = [
    "MIN_MEANINGFUL_CHARS",
    "QualityIssue",
    "QualityResult",
    "QualityStatus",
    "evaluate_chunks",
    "evaluate_text",
    "is_degraded",
]