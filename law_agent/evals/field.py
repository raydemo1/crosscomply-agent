"""Export reviewer-curated field drafts as field-split scenarios.

Field scenarios derive from real (de-identified) cases, so they are written
outside Git (default ``data/evals/field``). An exported scenario is never
overwritten: if its file changed after export, the export stops.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import date
from pathlib import Path

from law_agent.evals.bank.loader import FIELD_DIR, _read_dir
from law_agent.evals.bank.schema import (
    FactPatternSource,
    MaterialFile,
    Scenario,
    scenario_hash,
)

SIGNAL_LABELS = {
    "conclusion_not_useful": "审查人认为结论无用",
    "citation_wrong": "审查人判定引用错误",
    "missing_sources": "审查人指出缺少法源",
    "approval_rejected": "审批被驳回",
    "rereview_changed": "法规变更复审后结论改变",
}


def draft_scenario(draft: dict) -> Scenario:
    """Build the field scenario for one draft; raises ``ValidationError``."""

    created = draft.get("created_at")
    as_of = date.fromisoformat(str(created)[:10]) if created else date.today()
    kinds = [signal["kind"] for signal in draft["signals"]]
    return Scenario(
        scenario_id=f"field_{draft['id'][-12:]}", split="field", as_of_date=as_of,
        question=draft["question"], intake=draft["intake"],
        materials=[MaterialFile(filename="冻结材料.txt", kind="case_export", text=draft["material_text"])],
        fact_pattern_source=FactPatternSource(
            kind="field_feedback",
            public_facts="线上纠错信号：" + ("、".join(SIGNAL_LABELS.get(k, k) for k in kinds) or "无"),
            synthesized="问题、材料与申报信息经审查人人工脱敏",
        ),
        selection_reason=draft["selection_reason"], reference_basis=draft["reference_basis"],
        agent_rubric=draft["rubric"],
        tags=["field_feedback", *kinds],
    )


def load_field_scenarios(directory: Path = FIELD_DIR) -> list[Scenario]:
    return _read_dir(directory) if directory.is_dir() else []


def _write_atomic(path: Path, scenario: Scenario) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=path.parent, delete=False) as handle:
            pending = Path(handle.name)
            json.dump(scenario.model_dump(mode="json"), handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        pending.replace(path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def export_ready(store, output_dir: Path, sources: set[str], *, dry_run: bool = False) -> int:
    from law_agent.review.eval_feedback import validate_draft

    def write_cases(rows):
        existing = {s.scenario_id: s for s in load_field_scenarios(output_dir)}
        exported = {}
        pending: list[Scenario] = []
        for draft in rows:
            if draft["status"] == "exported":
                current = existing.get(draft["exported_case_id"])
                if current and scenario_hash(current) != draft["exported_sha256"]:
                    raise ValueError(f"已导出的场景文件已变化，未覆盖：{current.scenario_id}")
                continue
            scenario, errors = validate_draft(draft, sources)
            if errors:
                raise ValueError(f"草稿 {draft['id']} 未通过导出复验，请重新打开并检查")
            digest = scenario_hash(scenario)
            current = existing.get(scenario.scenario_id)
            if current and scenario_hash(current) != digest:
                raise ValueError(f"场景文件已变化，未覆盖：{scenario.scenario_id}")
            if not current:
                pending.append(scenario)
            exported[draft["id"]] = (scenario.scenario_id, digest)
        if not dry_run:
            for scenario in pending:
                _write_atomic(output_dir / f"{scenario.scenario_id}.json", scenario)
        return exported

    if dry_run:
        return len(write_cases([*store.list("ready"), *store.list("exported")]))
    return store.export_ready(write_cases)


def main(argv: list[str] | None = None) -> int:
    from law_agent.config import load_service_config
    from law_agent.review.eval_draft_store import PostgresEvalDraftStore
    from law_agent.review.eval_feedback import source_ids
    from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=FIELD_DIR)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CHUNKS_PATH.parent)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        count = export_ready(PostgresEvalDraftStore(load_service_config().postgres.dsn), args.output_dir,
                             source_ids(args.corpus), dry_run=args.dry_run)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{'Would export' if args.dry_run else 'Exported'} {count} field scenario(s)")
    return 0
