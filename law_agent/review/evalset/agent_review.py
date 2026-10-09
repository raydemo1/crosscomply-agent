"""Export an offline human-review package; never execute or score an Agent."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from law_agent.review.evalset.agent_cases import get_agent_cases
from law_agent.review.evalset.agent_cases_holdout import build_holdout_cases
from law_agent.review.evalset.agent_schemas import AgentCase
from law_agent.review.http.schemas import IntakePayload

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]


def review_groups() -> dict[str, list[AgentCase]]:
    groups = {"regression": get_agent_cases("core"), "holdout_candidate": build_holdout_cases()}
    seen: set[str] = set()
    inputs: set[tuple[str, str]] = set()
    for cases in groups.values():
        for case in cases:
            if case.case_id in seen or (case.question, case.material_text) in inputs:
                raise ValueError(f"duplicate review case: {case.case_id}")
            seen.add(case.case_id)
            inputs.add((case.question, case.material_text))
            unknown = set(case.intake) - IntakePayload.model_fields.keys()
            if unknown:
                raise ValueError(f"unknown intake fields in {case.case_id}: {sorted(unknown)}")
            IntakePayload.model_validate(case.intake)
            if not all((case.selection_reason, case.reference_basis,
                        case.rubric.allowed_judgments, case.rubric.forbidden_judgments)):
                raise ValueError(f"incomplete review rubric: {case.case_id}")
    return groups


def case_hash(case: AgentCase) -> str:
    value = json.dumps(case.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(value.encode()).hexdigest()


def export_review_package(output_dir: Path) -> dict:
    groups = review_groups()
    cases = [case for group in groups.values() for case in group]
    manifest = {
        "purpose": "offline_human_review",
        "model_calls": 0,
        "evaluation_performed": False,
        "case_counts": {name: len(group) for name, group in groups.items()},
        "approved_cases": sum(case.review_status == "approved" for case in cases),
        "case_hashes": {case.case_id: case_hash(case) for case in cases},
        "production_hashes": {
            str(path.relative_to(REPOSITORY_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((REPOSITORY_ROOT / "law_agent").rglob("*.py"))
            if "evalset" not in path.parts
        },
        "holdout_limitations": "自建合成、未实跑、待审定；选题参考历史失败，非独立盲测。用于调优后转入回归集。",
    }
    reviews = [
        {
            "case_id": case.case_id,
            "partition": name,
            "case_sha256": case_hash(case),
            "decision": "pending",
            "reviewer": "",
            "reviewed_at": "",
            "notes": "",
        }
        for name, group in groups.items() for case in group
    ]
    lines = [
        "# Agent 候选案例人工审定包", "",
        "本包未运行Agent、judge或模型API，没有PASS、准确率或正式Golden指标。", "",
        "审定者逐案核对事实是否充分、判断边界、关键法源及例外；填写review.json。",
        "decision填写approved、revise或rejected；记录审定者、日期和理由。",
        "审定结果须与case_sha256一致；改题后重新审定。填写记录不会自动修改源码的review_status。",
        "正式采用前将审定记录和对应题目一起纳入Git，并核对审批状态。", "",
    ]
    for name, group in groups.items():
        lines.extend([f"## {name}", ""])
        for case in group:
            lines.extend([
                f"### {case.case_id}", "", f"状态：{case.review_status}；SHA256：{case_hash(case)}", "",
                f"选题：{case.selection_reason}", "", f"问题：{case.question}", "",
                "材料：", "", case.material_text, "", "确认填报：", "", "```json",
                json.dumps(case.intake, ensure_ascii=False, indent=2), "```", "",
                "判断约束：", "", "```json",
                json.dumps(case.rubric.model_dump(mode="json"), ensure_ascii=False, indent=2),
                "```", "", "候选法源依据：", "",
            ])
            lines.extend(f"- {basis}" for basis in case.reference_basis)
            if case.scripted_answers:
                lines.extend(["", "预设即时答复（不自动升级为确认事实）：", "", "```json",
                              json.dumps([answer.model_dump(mode="json")
                                          for answer in case.scripted_answers],
                                         ensure_ascii=False, indent=2), "```"])
            if case.controlled_web:
                lines.extend(["", "受控法源留出/Web fixture：", "", "```json",
                              case.controlled_web.model_dump_json(indent=2), "```"])
            lines.append("")
    output_dir.mkdir(parents=True, exist_ok=False)
    for filename, value in {
        "manifest.json": manifest,
        "cases.json": {name: [case.model_dump(mode="json") for case in group]
                       for name, group in groups.items()},
        "review.json": reviews,
    }.items():
        (output_dir / filename).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
    (output_dir / "review.md").write_text("\n".join(lines), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    manifest = export_review_package(args.output_dir)
    print(json.dumps({"output_dir": str(args.output_dir.resolve()),
                      "case_counts": manifest["case_counts"], "model_calls": 0,
                      "evaluation_performed": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
