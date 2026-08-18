"""JSONL persistence helpers for review runs."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

from law_agent.data.io import read_jsonl, write_jsonl
from law_agent.review.schemas import RetrievalTrace, ReviewCase, ReviewResult

REVIEW_CASES_FILENAME = "review_cases.jsonl"
RETRIEVAL_TRACES_FILENAME = "retrieval_traces.jsonl"
REVIEW_RESULTS_FILENAME = "review_results.jsonl"


def review_cases_path(output_dir: Path) -> Path:
    return output_dir / REVIEW_CASES_FILENAME


def retrieval_traces_path(output_dir: Path) -> Path:
    return output_dir / RETRIEVAL_TRACES_FILENAME


def review_results_path(output_dir: Path) -> Path:
    return output_dir / REVIEW_RESULTS_FILENAME


def write_review_cases(path: Path, cases: Iterable[ReviewCase]) -> int:
    return write_jsonl(path, cases)


def read_review_cases(path: Path) -> list[ReviewCase]:
    return read_jsonl(path, ReviewCase)


def write_retrieval_traces(path: Path, traces: Iterable[RetrievalTrace]) -> int:
    return write_jsonl(path, traces)


def read_retrieval_traces(path: Path) -> list[RetrievalTrace]:
    return read_jsonl(path, RetrievalTrace)


def write_review_results(path: Path, results: Iterable[ReviewResult]) -> int:
    return write_jsonl(path, results)


def read_review_results(path: Path) -> list[ReviewResult]:
    return read_jsonl(path, ReviewResult)
