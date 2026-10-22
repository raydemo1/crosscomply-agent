"""Audit frozen QA evidence delivery without model requests or legal grading."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def audit_packet(history, fixed):
    evidence, updates, issues = {}, [], []
    expected = {item["chunk_id"]: item for item in fixed["citations"] if item.get("chunk_id")}
    for index, message in enumerate(history["messages"]):
        if message["role"] not in {"user", "tool"}:
            continue
        try:
            payload = json.loads(message.get("content") or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("state"), dict):
            continue
        for hit in payload["state"].get("evidence", []):
            chunk_id = hit["chunk_id"]
            evidence[chunk_id] = hit
            updates.append({"message_index": index, "chunk_id": chunk_id,
                "citation_role": hit.get("citation_role"), "can_cite_clause": hit.get("can_cite_clause")})
            for key in ("citation_role", "can_cite_clause"):
                if key not in hit:
                    issues.append({"message_index": index, "chunk_id": chunk_id, "issue": "missing_" + key})
    for chunk_id, citation in expected.items():
        hit = evidence.get(chunk_id)
        if hit is None:
            issues.append({"chunk_id": chunk_id, "issue": "missing_original"})
            continue
        for key in ("source_id", "citation_role", "can_cite_clause"):
            if hit.get(key) != citation.get(key):
                issues.append({"chunk_id": chunk_id, "issue": "mismatch_" + key})
        if (hit.get("full_article_text") or hit.get("text")) != citation["excerpt"]:
            issues.append({"chunk_id": chunk_id, "issue": "mismatch_original"})
    return {"expected_chunks": len(expected), "delivered_chunks": len(evidence),
        "updates": updates, "issues": issues, "protocol_matches_current": history.get("protocol_matches_current"),
        "limits": "Checks exact frozen originals and qualification delivery, not legal interpretation or causal effects of history."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    paths = {name: args.source / (name + ".json") for name in ("histories", "packets")}
    inputs = {name: json.loads(path.read_text(encoding="utf-8")) for name, path in paths.items()}
    result = {"input_sha256": {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()},
        "cases": {case: audit_packet(history, inputs["packets"][case]) for case, history in inputs["histories"].items()},
        "model_calls": 0}
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({case: {key: len(value) if key == "issues" else value for key, value in row.items()
        if key in {"expected_chunks", "delivered_chunks", "issues"}} for case, row in result["cases"].items()}))


if __name__ == "__main__":
    main()
