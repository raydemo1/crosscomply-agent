import json

from scripts.audit_qa_evidence_context import audit_packet


def packet(hit):
    return {"messages": [{"role": "tool", "content": json.dumps({"state": {"evidence": [hit]}})}]}


def test_exact_original_and_reference_qualification_remain_distinct():
    hit = {"chunk_id": "c", "source_id": "s", "text": "原文", "citation_role": "implementation_reference", "can_cite_clause": False}
    fixed = {"citations": [{**hit, "excerpt": "原文"}]}
    assert audit_packet(packet(hit), fixed)["issues"] == []
    altered = {**hit, "can_cite_clause": True}
    assert any(issue["issue"] == "mismatch_can_cite_clause" for issue in audit_packet(packet(altered), fixed)["issues"])


def test_truncated_original_and_absent_qualification_are_reported():
    fixed = {"citations": [{"chunk_id": "c", "source_id": "s", "excerpt": "完整原文", "can_cite_clause": False}]}
    result = audit_packet(packet({"chunk_id": "c", "source_id": "s", "text": "截断"}), fixed)
    assert {issue["issue"] for issue in result["issues"]} >= {"mismatch_original", "missing_can_cite_clause"}


def test_latest_chunk_update_and_missing_chunk_are_checked():
    hit = {"chunk_id": "c", "source_id": "s", "text": "原文", "citation_role": "implementation_reference", "can_cite_clause": False}
    history = packet(hit)
    history["messages"].extend(packet({**hit, "text": "改写"})["messages"])
    fixed = {"citations": [{**hit, "excerpt": "原文"}, {**hit, "chunk_id": "missing", "excerpt": "原文"}]}
    issues = audit_packet(history, fixed)["issues"]
    assert {item["issue"] for item in issues} == {"mismatch_original", "missing_original"}
