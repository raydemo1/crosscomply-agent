"""Export an offline human-review package; never execute or score an Agent."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from law_agent.review.evalset.agent_cases import get_agent_cases
from law_agent.review.evalset.agent_schemas import AgentCase
from law_agent.review.http.schemas import IntakePayload

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
FIELD_REVIEW_RECORD_PATH = Path(__file__).with_name("agent_case_review_field.json")


def review_case_errors(case: AgentCase) -> list[tuple[str, str]]:
    from pydantic import ValidationError

    errors = []
    if set(case.intake) - IntakePayload.model_fields.keys():
        errors.append(("intake", "申报信息包含未定义字段"))
    try:
        IntakePayload.model_validate(case.intake)
    except ValidationError as exc:
        errors.extend((f"intake.{error['loc'][0]}", "申报值类型或选项无效") for error in exc.errors(include_input=False))
    for field, values in (
        ("selection_reason", [case.selection_reason]),
        ("reference_basis", case.reference_basis),
        ("rubric.allowed_judgments", case.rubric.allowed_judgments),
        ("rubric.forbidden_judgments", case.rubric.forbidden_judgments),
    ):
        if not values or not all(value.strip() for value in values):
            errors.append((field, "请填写此项"))
    return errors


def _json_hash(value: dict) -> str:
    value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(value.encode()).hexdigest()


def case_hash(case: AgentCase) -> str:
    return _json_hash(case.model_dump(mode="json", exclude={"portfolio"} if case.portfolio is None else None))


def reviewed_selection(groups: dict[str, list[AgentCase]], *, group: str = "field") -> tuple[dict, dict]:
    if group != "field":
        raise ValueError(f"unknown review group: {group}")
    record = json.loads(FIELD_REVIEW_RECORD_PATH.read_text(encoding="utf-8"))
    cases = {case.case_id: case for group in groups.values() for case in group}
    entries = record["cases"]
    recorded = {entry["case_id"] for entry in entries}
    entries = [*entries, *[
        {"case_id": case.case_id, "case_sha256": case_hash(case),
         "next_run_reason": "新增待审定用例", "legal_review": "待人工审定", "findings_and_resolutions": []}
        for case in cases.values() if case.case_id not in recorded
    ]]
    record = {**record, "cases": entries}
    if len(entries) != len(cases) or {entry["case_id"] for entry in entries} != set(cases):
        raise ValueError("review record must cover every case exactly once")
    selected, skipped = [], []
    for entry in entries:
        case = cases[entry["case_id"]]
        if entry["case_sha256"] != case_hash(case):
            raise ValueError(f"case changed after review: {case.case_id}")
        item = {"case_id": case.case_id, "case_sha256": entry["case_sha256"]}
        if entry["next_run_reason"]:
            selected.append({**item, "reason": entry["next_run_reason"]})
        else:
            skipped.append({**item, "reason": "previous_pass_unchanged_judgment_boundary"})
    plan = {
        "purpose": "incremental_candidate_eval",
        "evaluation_performed": False,
        "review_record_sha256": _json_hash(record),
        "selected_count": len(selected),
        "skipped_count": len(skipped),
        "selected": selected,
        "skipped": skipped,
        "group": "field",
        "boundary": "现场纠错候选单独运行和报告；未通过独立审定的用例不是正式基准。",
    }
    return record, plan


def cases_from_selection(plan: dict) -> list[AgentCase]:
    if plan["purpose"] != "incremental_candidate_eval":
        raise ValueError("not an incremental candidate selection")
    group = plan.get("group", "field")
    groups = _package_groups(group)
    _record, expected = reviewed_selection(groups, group=group)
    if plan["review_record_sha256"] != expected["review_record_sha256"]:
        raise ValueError("review record changed; export a new selection")
    if plan["selected"] != expected["selected"] or plan["skipped"] != expected["skipped"]:
        raise ValueError("selection differs from the reviewed case list")
    cases = {case.case_id: case for group in groups.values() for case in group}
    return [cases[item["case_id"]] for item in plan["selected"]]


def _package_groups(group):
    if group != "field":
        raise ValueError(f"unknown review group: {group}")
    cases = get_agent_cases("field")
    if len({case.case_id for case in cases}) != len(cases):
        raise ValueError("duplicate field review case")
    for case in cases:
        if errors := review_case_errors(case):
            raise ValueError(f"incomplete review case {case.case_id}: {errors}")
    return {"field": cases}


def export_review_package(output_dir: Path, *, group: str = "field") -> dict:
    groups = _package_groups(group)
    record, selection = reviewed_selection(groups, group=group)
    cases = [case for group in groups.values() for case in group]
    manifest = {
        "purpose": "offline_human_review",
        "model_calls": 0,
        "evaluation_performed": False,
        "case_counts": {name: len(group) for name, group in groups.items()},
        "approved_cases": sum(case.review_status == "approved" for case in cases),
        "ai_review_kind": record["review_kind"],
        "independent_human_approval": record["independent_human_approval"],
        "case_hashes": {case.case_id: case_hash(case) for case in cases},
        "production_hashes": {
            str(path.relative_to(REPOSITORY_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted((REPOSITORY_ROOT / "law_agent").rglob("*.py"))
            if "evalset" not in path.parts
        },
        "holdout_limitations": "线上复核信号经脱敏形成的候选；与合成用例分开报告。",
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
        f"AI逐案复核见ai_review.json；增量选择{selection['selected_count']}案、暂不重跑{selection['skipped_count']}案。",
        "selection.json只冻结本次选题，不把历史PASS合并为当前版本的新总体分数。", "",
    ]
    for name, group_cases in groups.items():
        lines.extend([f"## {name}", ""])
        for case in group_cases:
            review = next(entry for entry in record["cases"] if entry["case_id"] == case.case_id)
            lines.extend([
                f"### {case.case_id}", "", f"状态：{case.review_status}；SHA256：{case_hash(case)}", "",
                f"AI复核：{review['legal_review']}", "",
                f"后续运行：{review['next_run_reason'] or '边界未变且此前通过，本批跳过'}", "",
                "复核发现及处理：", "",
                *[f"- {finding}" for finding in review["findings_and_resolutions"]], "",
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
        "ai_review.json": record,
        "selection.json": selection,
    }.items():
        (output_dir / filename).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
        )
    (output_dir / "review.md").write_text("\n".join(lines), encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--group", choices=["field"], default="field")
    args = parser.parse_args()
    manifest = export_review_package(args.output_dir, group=args.group)
    print(json.dumps({"output_dir": str(args.output_dir.resolve()),
                      "case_counts": manifest["case_counts"], "model_calls": 0,
                      "evaluation_performed": False}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
