"""File IO helpers for JSONL and manifests."""

from __future__ import annotations

import csv
import json
from collections.abc import Iterable
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from law_agent.data.schemas import SourceRecord

T = TypeVar("T", bound=BaseModel)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_jsonl(path: Path, records: Iterable[BaseModel]) -> int:
    ensure_parent(path)
    count = 0
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(record.model_dump_json(exclude_none=True) + "\n")
            count += 1
    return count


def read_jsonl(path: Path, model: type[T]) -> list[T]:
    records: list[T] = []
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                records.append(model.model_validate_json(line))
    return records


def read_manifest(path: Path) -> list[SourceRecord]:
    # ``csv`` defaults to 128 KiB fields. A compatibility read is required
    # before older manifests can be rewritten in the compact form below.
    csv.field_size_limit(10_000_000)
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        return [SourceRecord.model_validate(row) for row in reader]


def write_manifest(path: Path, records: Iterable[SourceRecord]) -> int:
    ensure_parent(path)
    fields = list(SourceRecord.model_fields.keys())
    count = 0
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for record in records:
            row = record.model_dump(mode="json")
            for field in ("topic_tags", "legal_domain", "applicable_subjects", "contract_parties"):
                row[field] = ";".join(getattr(record, field))
            writer.writerow(row)
            count += 1
    return count


def write_json(path: Path, data: object) -> None:
    ensure_parent(path)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
