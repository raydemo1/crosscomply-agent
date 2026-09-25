"""Copy the active corpus's reviewed citation roles into its source manifest once."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from law_agent.data.schemas import ClauseCitationRole


def migrate(corpus: Path) -> int:
    manifest = corpus / "source_manifest.csv"
    chunks = corpus / "chunks.jsonl"
    roles: dict[str, ClauseCitationRole] = {}
    with chunks.open("r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            chunk = json.loads(line)
            source_id = chunk["source_id"]
            role = chunk["citation_role"]
            previous = roles.setdefault(source_id, role)
            if previous != role:
                raise ValueError(f"同一来源的 chunk 引用角色不一致：{source_id}")

    with manifest.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        rows = list(reader)
    if "citation_role" in fields:
        raise ValueError("清单已有 citation_role；请检查后再操作")
    missing = {row["source_id"] for row in rows} - roles.keys()
    if missing:
        raise ValueError(f"以下来源没有可继承的 chunk：{', '.join(sorted(missing))}")
    backup = manifest.with_suffix(".before-citation-role.csv")
    if backup.exists():
        raise ValueError(f"备份文件已存在：{backup}")
    temporary = manifest.with_suffix(".citation-role.tmp")
    for row in rows:
        row["citation_role"] = roles[row["source_id"]]
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=[*fields, "citation_role"])
        writer.writeheader()
        writer.writerows(rows)
    manifest.replace(backup)
    temporary.replace(manifest)
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("corpus", type=Path)
    args = parser.parse_args()
    count = migrate(args.corpus)
    print(f"已将 {count} 个来源的引用角色写入法源清单")


if __name__ == "__main__":
    main()
