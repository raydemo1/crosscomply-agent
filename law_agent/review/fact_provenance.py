"""Source observations alongside the Agent's working fact projection."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

from pydantic import JsonValue

from law_agent.review.schemas import FactLedgerEntry, MaterialFactObservation


def _has_value(value: JsonValue) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip()) and value.strip().lower() != "unknown"
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def confirmed_intake_ledger(
    intake: Mapping[str, JsonValue], snapshot_id: str,
) -> list[FactLedgerEntry]:
    return [
        FactLedgerEntry(
            field=field, value=value, source_type="confirmed_intake",
            source_ref=snapshot_id, status="confirmed",
        )
        for field, value in intake.items() if _has_value(value)
    ]


def append_fact(ledger: list[FactLedgerEntry], entry: FactLedgerEntry) -> None:
    value_key = json.dumps(entry.value, sort_keys=True, ensure_ascii=False)
    if any(
        item.field == entry.field and item.source_type == entry.source_type
        and item.source_ref == entry.source_ref
        and json.dumps(item.value, sort_keys=True, ensure_ascii=False) == value_key
        for item in ledger
    ):
        return
    if entry.source_type in {"confirmed_intake", "material"}:
        for item in ledger:
            if (
                item.field == entry.field
                and {item.source_type, entry.source_type} == {"confirmed_intake", "material"}
                and _opposite_values(entry.field, item.value, entry.value)
            ):
                item.status = entry.status = "conflicted"
    ledger.append(entry)


def _opposite_values(field: str, left: JsonValue, right: JsonValue) -> bool:
    if type(left) is bool and type(right) is bool:
        return left != right
    if isinstance(left, str) and isinstance(right, str):
        opposite = {
            "ciio_status": {"ciio", "not_ciio"},
            "important_data_status": {"important", "not_important"},
        }.get(field)
        return opposite is not None and {left, right} == opposite
    return False


def record_material_facts(
    ledger: list[FactLedgerEntry], observations: Sequence[MaterialFactObservation],
    snapshot_id: str | None,
) -> None:
    if observations and not snapshot_id:
        raise ValueError("材料事实来源缺少冻结材料快照")
    for observation in observations:
        if _has_value(observation.value):
            append_fact(ledger, FactLedgerEntry(
                field=observation.field, value=observation.value, source_type="material",
                source_ref=snapshot_id, status="extracted",
            ))
