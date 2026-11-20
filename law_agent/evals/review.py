"""Human review of scenario labels and the locked test split.

Review records bind a decision to a scenario hash; editing a scenario after
review invalidates the record. The program never invents a reviewer: it only
copies decisions a human filled into an exported package.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from law_agent.data.schemas import StrictModel
from law_agent.evals.bank.loader import BANK_DIR, write_scenario
from law_agent.evals.bank.schema import Scenario, render_materials, scenario_hash

REVIEWS_PATH = BANK_DIR / "reviews.json"
TEST_LOCK_PATH = BANK_DIR / "test_lock.json"


class ReviewRecord(StrictModel):
    scenario_id: str
    scenario_sha256: str
    decision: Literal["approved", "revise", "rejected"]
    reviewer: str = Field(min_length=1)
    reviewed_at: str = Field(min_length=1)
    notes: str = ""


def load_reviews(path: Path = REVIEWS_PATH) -> list[ReviewRecord]:
    if not path.exists():
        return []
    return [ReviewRecord.model_validate(row) for row in json.loads(path.read_text(encoding="utf-8"))]


def load_test_lock(path: Path = TEST_LOCK_PATH) -> dict[str, str] | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))["scenarios"]


def review_errors(
    scenarios: list[Scenario], *, reviews_path: Path = REVIEWS_PATH, lock_path: Path = TEST_LOCK_PATH,
) -> list[str]:
    errors: list[str] = []
    latest = {record.scenario_id: record for record in load_reviews(reviews_path)}
    for scenario in scenarios:
        if scenario.label_status != "approved":
            continue
        record = latest.get(scenario.scenario_id)
        if record is None or record.decision != "approved":
            errors.append(f"{scenario.scenario_id}: 标为 approved 但没有对应的审定记录")
        elif record.scenario_sha256 != scenario_hash(scenario):
            errors.append(f"{scenario.scenario_id}: 场景在审定后被修改，需要重新审定")
        elif record.reviewer != scenario.reviewer:
            errors.append(f"{scenario.scenario_id}: 审定人与审定记录不一致")
    lock = load_test_lock(lock_path)
    if lock is not None:
        current = {s.scenario_id: scenario_hash(s) for s in scenarios if s.split == "test"}
        for scenario_id, digest in lock.items():
            if scenario_id not in current:
                errors.append(f"{scenario_id}: 已锁定的 test 场景不存在或被移出 test")
            elif current[scenario_id] != digest:
                errors.append(f"{scenario_id}: 已锁定的 test 场景被修改，需要重新审定并重新锁定")
        errors.extend(f"{scenario_id}: test 场景未锁定" for scenario_id in current if scenario_id not in lock)
    return errors


def export_review_package(scenarios: list[Scenario], output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "purpose": "offline_human_review",
        "model_calls": 0,
        "evaluation_performed": False,
        "created_at": datetime.now(UTC).isoformat(),
        "counts": {split: sum(s.split == split for s in scenarios) for split in sorted({s.split for s in scenarios})},
        "scenario_hashes": {s.scenario_id: scenario_hash(s) for s in scenarios},
    }
    template = [{
        "scenario_id": s.scenario_id, "scenario_sha256": scenario_hash(s),
        "decision": "", "reviewer": "", "reviewed_at": "", "notes": "",
    } for s in scenarios]
    lines = [
        "# 评测场景人工审定包", "",
        "本包未运行任何模型。逐个场景核对材料是否真实可信、事实模式出处、检索标签与判断边界。",
        "在 review.json 中填写 decision（approved / revise / rejected）、reviewer、reviewed_at（YYYY-MM-DD）和理由；",
        "然后运行 `python -m law_agent.evals record-review <review.json>`。场景改动后散列变化，需要重新审定。", "",
    ]
    for s in scenarios:
        lines += [
            f"## {s.scenario_id}（{s.split}）", "", f"散列：{scenario_hash(s)}", "",
            f"审查时点：{s.as_of_date}", "", f"问题：{s.question}", "",
            f"事实模式出处：{s.fact_pattern_source.kind}；{s.fact_pattern_source.public_facts}", "",
            f"虚构部分：{s.fact_pattern_source.synthesized}", "",
            "确认填报：", "", "```json", json.dumps(s.intake, ensure_ascii=False, indent=2), "```", "",
            "材料：", "", "```text", render_materials(s), "```", "",
        ]
        if s.retrieval:
            lines += ["检索标签：", "", "```json", s.retrieval.model_dump_json(indent=2), "```", ""]
        if s.agent_rubric:
            lines += ["判断边界：", "", "```json", s.agent_rubric.model_dump_json(indent=2), "```", ""]
    files = {
        "manifest.json": manifest,
        "scenarios.json": [s.model_dump(mode="json") for s in scenarios],
        "review.json": template,
    }
    for name, value in files.items():
        (output_dir / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output_dir / "review.md").write_text("\n".join(lines), encoding="utf-8")
    return manifest


def record_review(
    filled: Path, scenarios: list[Scenario], *, bank_dir: Path = BANK_DIR, reviews_path: Path = REVIEWS_PATH,
) -> int:
    """Copy human decisions into the review log and flip approved scenarios."""

    rows = [row for row in json.loads(filled.read_text(encoding="utf-8")) if row.get("decision")]
    records = [ReviewRecord.model_validate(row) for row in rows]
    by_id = {s.scenario_id: s for s in scenarios}
    for record in records:
        scenario = by_id.get(record.scenario_id)
        if scenario is None:
            raise ValueError(f"{record.scenario_id}: 场景不存在")
        if record.scenario_sha256 != scenario_hash(scenario):
            raise ValueError(f"{record.scenario_id}: 审定后场景已被修改，请重新导出审定包")
        if scenario.split == "field":
            raise ValueError(f"{record.scenario_id}: 现场场景不在 Git 中，审定记录请随导出文件保存")
    log = [*load_reviews(reviews_path), *records]
    reviews_path.write_text(
        json.dumps([r.model_dump() for r in log], ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    for record in records:
        scenario = by_id[record.scenario_id]
        approved = record.decision == "approved"
        updated = scenario.model_copy(update={
            "label_status": "approved" if approved else "draft",
            "reviewer": record.reviewer if approved else None,
        })
        write_scenario(updated, bank_dir / "scenarios")
    return len(records)


def lock_test_split(
    scenarios: list[Scenario], *, allow_draft: bool = False, lock_path: Path = TEST_LOCK_PATH,
) -> int:
    tests = [s for s in scenarios if s.split == "test"]
    drafts = [s.scenario_id for s in tests if s.label_status != "approved"]
    if drafts and not allow_draft:
        raise ValueError(f"以下 test 场景尚未审定，不能锁定：{drafts}")
    lock_path.write_text(json.dumps({
        "locked_at": datetime.now(UTC).isoformat(),
        "includes_drafts": bool(drafts),
        "scenarios": {s.scenario_id: scenario_hash(s) for s in tests},
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(tests)
