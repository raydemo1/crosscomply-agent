"""Export ready human-reviewed drafts as candidate AgentCase fixtures."""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from pydantic import TypeAdapter

from law_agent.review.evalset.agent_schemas import AgentCase

FIELD_CASES_PATH = Path(__file__).with_name("agent_cases_field.json")


def load_field_cases(path: Path = FIELD_CASES_PATH) -> list[AgentCase]:
    return TypeAdapter(list[AgentCase]).validate_json(path.read_text(encoding="utf-8")) if path.exists() else []


def export_ready(store, output, sources, *, dry_run=False):
    from law_agent.review.eval_feedback import validate_draft
    from law_agent.review.evalset.agent_review import case_hash

    def write_cases(rows):
        previous = load_field_cases(output)
        merged = {case.case_id: case for case in previous}
        if len(merged) != len(previous):
            raise ValueError("目标文件存在重复的评测用例 ID")
        exported = {}
        modified = False
        for draft in rows:
            if draft["status"] == "exported":
                existing = merged.get(draft["exported_case_id"])
                if existing and case_hash(existing) != draft["exported_sha256"]:
                    raise ValueError(f"目标文件中的已导出用例已变化，未覆盖：{existing.case_id}")
                continue
            candidate, errors = validate_draft(draft, sources)
            if errors:
                raise ValueError(f"草稿 {draft['id']} 未通过导出复验，请重新打开并检查")
            existing = merged.get(candidate.case_id)
            digest = case_hash(candidate)
            if existing and case_hash(existing) != digest:
                raise ValueError(f"目标文件中的用例已变化，未覆盖：{candidate.case_id}")
            if not existing:
                merged[candidate.case_id] = candidate
                modified = True
            exported[draft["id"]] = (candidate.case_id, digest)
        if modified and not dry_run:
            output.parent.mkdir(parents=True, exist_ok=True)
            pending = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", encoding="utf-8", newline="\n", dir=output.parent, delete=False,
                ) as handle:
                    pending = Path(handle.name)
                    json.dump([merged[key].model_dump(mode="json") for key in sorted(merged)],
                              handle, ensure_ascii=False, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                pending.replace(output)
            finally:
                if pending is not None:
                    pending.unlink(missing_ok=True)
        return exported

    return len(write_cases([*store.list("ready"), *store.list("exported")])) if dry_run else store.export_ready(write_cases)


def main() -> int:
    from law_agent.config import load_service_config
    from law_agent.review.eval_draft_store import PostgresEvalDraftStore
    from law_agent.review.eval_feedback import source_ids
    from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--output", type=Path, default=FIELD_CASES_PATH)
    export.add_argument("--corpus", type=Path, default=DEFAULT_CHUNKS_PATH.parent)
    export.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        count = export_ready(PostgresEvalDraftStore(load_service_config().postgres.dsn), args.output,
                             source_ids(args.corpus), dry_run=args.dry_run)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(f"{'Would export' if args.dry_run else 'Exported'} {count} candidate case(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
