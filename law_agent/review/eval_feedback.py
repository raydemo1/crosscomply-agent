"""Human correction signals and deterministic field-candidate validation."""

import re
from pathlib import Path

from pydantic import ValidationError

from law_agent.data.io import read_manifest
from law_agent.review.evalset.agent_review import review_case_errors
from law_agent.review.evalset.agent_schemas import AgentCase, AgentRubric
from law_agent.review.http.schemas import IntakePayload
from law_agent.review.material_search import format_frozen_material

PATTERNS = (
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("身份证号", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("邮箱", re.compile(r"[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9-]+(?:\.[A-Z0-9-]+)+", re.IGNORECASE)),
    ("统一社会信用代码", re.compile(r"(?<![0-9A-Z])[0-9A-HJ-NPQRTUWXY]{2}\d{6}[0-9A-HJ-NPQRTUWXY]{10}(?![0-9A-Z])", re.IGNORECASE)),
)


def candidate_signals(case_id, *, cases, governance, impact=None):
    feedback = [row for row in cases.list_feedback(case_id) if row["actor_role"] in {"reviewer", "admin"}]
    signals = []
    if any(row["conclusion_useful"] is False for row in feedback):
        signals.append({"kind": "conclusion_not_useful"})
    refs = sorted({ref for row in feedback for ref, verdict in row["citation_verdicts"].items() if verdict == "wrong"})
    if refs:
        signals.append({"kind": "citation_wrong", "citation_refs": refs})
    if any((row["missing_sources"] or "").strip() for row in feedback):
        signals.append({"kind": "missing_sources"})
    approval = governance.get_latest_approval(case_id)
    if approval and approval.status == "rejected":
        signals.append({"kind": "approval_rejected"})
    if impact:
        original = cases.get_case(case_id)
        original_result = (original.get("response") or {}).get("review_result") or {}
        for flag in impact.flags(case_id=case_id, status="resolved"):
            case = cases.get_case(flag["rereview_case_id"])
            result = ((case or {}).get("response") or {}).get("review_result") or {}
            if original_result and result and any(result.get(key) != original_result.get(key) for key in ("risk_level", "legal_path")):
                signals.append({"kind": "rereview_changed"})
                break
    return signals


def draft_inputs(case, task, *, enterprise, transfers):
    material = enterprise.get_material_snapshot(task.material_snapshot_id)
    intake = enterprise.get_intake_snapshot(task.intake_snapshot_id)
    if (not material or not intake or material.case_id != case["id"] or intake.case_id != case["id"]
            or intake.material_snapshot_id != material.id):
        raise ValueError("审查任务缺少匹配的冻结材料或事实")
    versions = [enterprise.get_material_version(identifier) for identifier in material.version_ids]
    if any(not row or row.case_id != case["id"] or not (row.parsed_text or "").strip() for row in versions):
        raise ValueError("审查任务的冻结材料不完整")
    payload = {key: value for key, value in intake.intake.items() if key != "followup_answers"}
    activity_id = transfers.for_case(case["id"])
    designation = transfers.review_for_case(case["id"]) or (transfers.get(activity_id) if activity_id else None)
    entities = {str(payload.get("business_entity") or "").strip()}
    if designation:
        entities.add(designation["business_entity"].strip())
    result = (task.result or {}).get("review_result") or {}
    output = {key: result.get(key) for key in ("risk_level", "legal_path", "decision_summary", "conclusion")}
    output["citations"] = [{key: citation.get(key) for key in ("source_id", "chunk_id", "article_no")}
                           for citation in result.get("citations") or []]
    return {"question": case["question"], "material_text": format_frozen_material(versions),
            "intake": payload, "agent_output": output, "original_entities": sorted(entities - {""})}


def source_ids(corpus: Path) -> set[str]:
    manifest = corpus / "source_manifest.csv"
    return {row.source_id for row in read_manifest(manifest)} if manifest.exists() else set()


def _strings(value, field):
    if isinstance(value, str):
        yield field, value
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _strings(item, f"{field}[{index}]")
    elif isinstance(value, dict):
        known = {"rubric": AgentRubric.model_fields, "intake": IntakePayload.model_fields}.get(field, {})
        for index, (key, item) in enumerate(value.items()):
            yield f"{field}[键 {index + 1}]", key
            yield from _strings(item, f"{field}.{key}" if key in known else field)


def validate_draft(draft, sources):
    errors = []

    def add(field, message, **detail):
        errors.append({"field": field, "message": message, **detail})

    case = None
    try:
        case = AgentCase(
            case_id=f"field_{draft['id'][-12:]}", review_status="candidate", source_case_id=None,
            construction_round=1, question=draft["question"], material_text=draft["material_text"],
            intake=draft["intake"], rubric=draft["rubric"], selection_reason=draft["selection_reason"],
            reference_basis=draft["reference_basis"],
            tags=["candidate", "field_feedback", *(signal["kind"] for signal in draft["signals"])],
        )
    except ValidationError as exc:
        for error in exc.errors(include_input=False, include_context=False):
            loc = error["loc"]
            field = str(loc[0])
            if field == "rubric" and len(loc) > 1 and loc[1] in AgentRubric.model_fields:
                field += f".{loc[1]}"
            add(field, "字段类型或取值无效")
    if not draft["deidentified"]:
        add("deidentified", "请先人工检查并确认已脱敏")
    for field in ("question", "material_text"):
        if not draft[field].strip():
            add(field, "请填写此项")
    if "followup_answers" in draft["intake"]:
        add("intake", "评测草稿不得包含原案补充回答")
    if case:
        for field, message in review_case_errors(case):
            add(field, message)
        for field in ("must_cover_sources", "forbidden_clause_sources"):
            for index, identifier in enumerate(getattr(case.rubric, field)):
                if identifier not in sources:
                    add(f"rubric.{field}", f"第 {index + 1} 项法源不在当前语料清单")
    patterns = (*PATTERNS, *(("原申报主体", re.compile(re.escape(name), re.IGNORECASE)) for name in draft["original_entities"] if name))
    for field in ("question", "material_text", "intake", "rubric", "selection_reason", "reference_basis"):
        for location, text in _strings(draft[field], field):
            for kind, pattern in patterns:
                for match in pattern.finditer(text):
                    add(location, f"疑似残留{kind}", offset=match.start(), masked="*" * min(len(match[0]), 18))
                    if len(errors) >= 100:
                        return case, errors
    return case, errors
