"""Durable evaluation runs: one directory per run, written as it goes.

A run directory never overwrites an earlier one. ``manifest.json`` pins the
code, corpus, scenarios and model configuration; every arm × scenario gets
its own folder with the delivery, grade and event log, flushed on write so an
interrupted run still leaves evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from law_agent.evals.bank.schema import Scenario, scenario_hash

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BLOCKING_HTTP = (401, 402, 403, 429)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def infrastructure_http_status(message: str) -> int | None:
    """HTTP status of a provider failure that is not the system's fault."""

    match = re.search(r"\bHTTP (401|402|403|408|429|5\d\d)\b", message or "")
    return int(match.group(1)) if match else None


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2, default=str)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _sha256_file(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True, cwd=REPOSITORY_ROOT).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def code_fingerprint() -> dict[str, Any]:
    production = sorted(p for p in (REPOSITORY_ROOT / "law_agent").rglob("*.py") if "evals" not in p.parts)
    digest = hashlib.sha256()
    for path in production:
        digest.update(str(path.relative_to(REPOSITORY_ROOT)).encode())
        digest.update(path.read_bytes())
    return {
        "base_commit": _git("rev-parse", "HEAD"),
        "working_tree_dirty": bool(_git("status", "--porcelain")),
        "production_sha256": digest.hexdigest(),
    }


class RunStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=False)

    def write_manifest(
        self, *, kind: str, scenarios: list[Scenario], config: dict[str, Any], chunks_path: Path,
    ) -> dict[str, Any]:
        manifest = {
            "kind": kind,
            "started_at": utc_now(),
            **code_fingerprint(),
            "corpus_sha256": _sha256_file(chunks_path),
            "source_manifest_sha256": _sha256_file(chunks_path.with_name("source_manifest.csv")),
            "config": config,
            "scenarios": {s.scenario_id: {"sha256": scenario_hash(s), "split": s.split,
                                          "label_status": s.label_status} for s in scenarios},
        }
        write_json(self.root / "manifest.json", manifest)
        return manifest

    def case_dir(self, arm: str, scenario_id: str) -> Path:
        path = self.root / arm / scenario_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def event(self, directory: Path, kind: str, value: object) -> None:
        with (directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"at": utc_now(), "kind": kind, "value": value},
                                    ensure_ascii=False, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def save(self, directory: Path, name: str, value: object) -> None:
        write_json(directory / name, value)

    def finish(self, summary: dict[str, Any]) -> None:
        write_json(self.root / "summary.json", {"finished_at": utc_now(), **summary})


def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float] | None:
    """95% Wilson score interval; honest for the small samples this suite has."""

    if total == 0:
        return None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, centre - margin), min(1.0, centre + margin)
