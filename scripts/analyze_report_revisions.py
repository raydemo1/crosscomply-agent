"""Audit saved Agent revisions and context continuity without calling services or models."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from law_agent.review.result_builder import LLMReviewResultDraft


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit_run(root: Path) -> dict:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    events = [json.loads(line) for line in (root / "model_calls.jsonl").read_text(encoding="utf-8").splitlines()]
    starts = {e["call_id"]: e for e in events if e["event"] == "started"}
    completed = {e["call_id"]: e for e in events if e["event"] == "completed"}
    cases = []
    for case in manifest["production_cases"]:
        case_id = case["case_id"]
        final_state = json.loads((root / case_id / "state.json").read_text(encoding="utf-8"))
        final_evidence = {hit["chunk_id"]: hit for hit in final_state["evidence"]}
        final_steps = {step["number"]: step for step in final_state["steps"]}
        agent_calls = []
        for call_id, event in starts.items():
            if event["task"] != case_id or call_id not in completed:
                continue
            messages = event["request"]["messages"]
            if not messages[0]["content"].startswith("你是企业数据合规执行 Agent"):
                continue
            agent_calls.append({"call_id": call_id, "state": json.loads(messages[1]["content"])["state"],
                                "decision": json.loads(completed[call_id]["choices"][0]["content"])})
        revisions = []
        continuity = []
        previous_draft = None
        previous_step = None
        for position, call in enumerate(agent_calls):
            state = call["state"]
            available = {}
            for hit in state["evidence"]:
                restored = {**state.get("evidence_sources", {}).get(hit["source_id"], {}), **hit}
                available[hit["chunk_id"]] = restored
            past_steps = state["steps"]
            read_ids = {cid for step in past_steps for entry in step["observation"].get("read_results", [])
                        for cid in entry.get("chunk_ids", [])}
            text_matches = all(
                cid in final_evidence and all(hit.get(key) == final_evidence[cid].get(key) for key in (
                    "text", "source_id", "can_cite_clause", "citation_role", "article_no", "heading_path",
                )) for cid, hit in available.items()
            )
            feedback = next((step["observation"].get("semantic_grounding") for step in past_steps
                             if step["number"] == previous_step), None)
            expected_feedback = (final_steps[previous_step]["observation"].get("semantic_grounding")
                                 if previous_step is not None else None)
            expected_material = {step["number"]: step["observation"]["text"] for step in final_state["steps"]
                                 if step["action"] == "read_material" and step["number"] < state["turns"]}
            actual_material = {step["number"]: step["observation"]["text"] for step in past_steps
                               if step["action"] == "read_material"}
            continuity.append({
                "call_id": call["call_id"], "turn": state["turns"], "evidence_count": len(available),
                "previous_draft_complete": previous_draft is None or state.get("report_draft") == previous_draft,
                "previous_feedback_complete": feedback == expected_feedback,
                "material_complete": actual_material == expected_material,
                "read_text_complete": read_ids <= available.keys(), "evidence_text_and_qualification_match": text_matches,
            })
            if call["decision"]["action"] != "finish":
                continue
            draft = LLMReviewResultDraft.model_validate(call["decision"]["draft"]).model_dump(mode="json")
            step = final_steps[state["turns"]]
            next_id = agent_calls[position + 1]["call_id"] if position + 1 < len(agent_calls) else float("inf")
            verifier_calls = []
            for call_id, event in starts.items():
                if not call["call_id"] < call_id < next_id or event["task"] != case_id:
                    continue
                messages = event["request"]["messages"]
                if not messages[0]["content"].startswith(("你是独立法律证据校验员", "你核对的是报告单元")):
                    continue
                payload = json.loads(messages[1]["content"])
                authorities = payload.get("authorities", [*payload.get("cited_authorities", []), *payload.get("context_authorities", [])])
                original_text_ids = {item["chunk_id"] for item in authorities}
                verifier_calls.append({
                    "call_id": call_id, "retry": len(messages) > 2,
                    "retry_feedback": messages[-1]["content"] if len(messages) > 2 else None,
                    "authorities_returned_to_agent": original_text_ids <= available.keys(),
                    "authority_texts_exposed_to_agent": all(
                        item["chunk_id"] in available and item["article_text"] in (
                            available[item["chunk_id"]]["text"],
                            available[item["chunk_id"]].get("full_article_text"),
                        ) for item in authorities
                    ),
                    "authority_texts": {item["chunk_id"]: item["article_text"] for item in authorities},
                    "response": json.loads(completed[call_id]["choices"][0]["content"]) if call_id in completed else None,
                })
            revisions.append({
                "submission": len(revisions) + 1, "step": state["turns"], "call_id": call["call_id"],
                "draft": draft, "changed_fields": [key for key in draft if previous_draft is not None and draft[key] != previous_draft[key]],
                "feedback": step["observation"].get("semantic_grounding"),
                "error": step["observation"].get("error"),
                "completed": step["observation"].get("completed", False), "verifier_calls": verifier_calls,
            })
            previous_draft = draft
            previous_step = state["turns"] if step["observation"].get("semantic_grounding") else None
        cases.append({"case_id": case_id, "goal": case["question"], "continuity": continuity, "revisions": revisions})
    return {"base_commit": manifest["base_commit"], "run": root.as_posix(), "cases": cases,
            "artifacts_sha256": {str(path.relative_to(root)): digest(path) for path in [root / "manifest.json", root / "model_calls.jsonl", *root.glob("*/state.json")]},
            "limits": "Saved outputs and input continuity only; classifications require manual review. No legal regrading or model invocation."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = audit_run(args.run)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for case in report["cases"]:
        print(json.dumps({"case_id": case["case_id"], "submissions": len(case["revisions"]),
                          "continuity_pass": all(all(value for key, value in row.items() if key not in {"call_id", "turn", "evidence_count"}) for row in case["continuity"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
