"""Original Q&A inputs and run-scoped history, independent of review facts."""

from __future__ import annotations

from copy import deepcopy

from law_agent.review.agent_context import compact_evidence_context
from law_agent.review.qa_citations import register_citation_marker


def qa_state_context(state):
    payload = compact_evidence_context(state)
    payload.pop("citation_markers", None)
    markers = state.get("citation_markers", {})
    for hit in payload["evidence"]:
        marker = register_citation_marker(f"chunk:{hit['chunk_id']}", markers)
        hit["citation_marker"] = marker
        hit["citation_label"] = f"{marker} {hit.get('citation_label') or hit.get('article_no') or hit['source_id']}"
    facts = payload.pop("facts")
    payload.pop("material_preview", None)
    exclusions = payload.pop("source_scope_exclusions", [])
    payload["research_context"] = {
        key: facts[key] for key in ("regions", "industry", "as_of_date")
        if facts[key] not in (None, [])
    }
    if exclusions:
        payload["research_context"]["excluded_source_scopes"] = exclusions
    return payload


def material_package(materials):
    return "\n\n".join(f"【材料 {m['filename']} | {m['id']}】\n{m['parsed_text']}" for m in materials)


def material_passages(state, materials):
    package = material_package(materials)
    windows = []
    observations = [state.get("material_preview", {}),
                    *(step["observation"] for step in state.get("steps", []) if step["action"] == "read_material")]
    for observation in observations:
        text = observation.get("text")
        offset = observation.get("offset")
        if isinstance(text, str) and type(offset) is int and offset >= 0 and package[offset:offset + len(text)] == text:
            windows.append((offset, offset + len(text)))
    cursor = 0
    passages = []
    for material in materials:
        start = cursor + len(f"【材料 {material['filename']} | {material['id']}】\n")
        end = start + len(material["parsed_text"])
        for left, right in windows:
            a, b = max(left, start), min(right, end)
            if a < b:
                passages.append({"material_version_id": material["id"], "filename": material["filename"],
                                 "offset": a - start, "text": package[a:b]})
        cursor = end + 2
    return merge_material_passages(passages)


def merge_material_passages(passages):
    merged = []
    for passage in sorted(passages, key=lambda p: (p["material_version_id"], p["offset"])):
        if merged and merged[-1]["material_version_id"] == passage["material_version_id"]:
            previous = merged[-1]
            end = previous["offset"] + len(previous["text"])
            if passage["offset"] <= end:
                previous["text"] += passage["text"][max(0, end - passage["offset"]):]
                continue
        merged.append(dict(passage))
    return merged


def conversation_context(conversation, run):
    previous = []
    for item in conversation["runs"]:
        if item["id"] == run["id"]:
            break
        previous.append(item)
    selected = set(run["material_ids"])
    history, passages = [], []
    for item in previous:
        entry = {"turn_id": item["id"], "user_statement": item["question"]}
        if item["status"] == "completed" and set(item["material_ids"]) <= selected:
            entry["previous_model_answer"] = item["answer"]
            if (item["result"] or {}).get("questions"):
                entry["previous_model_questions"] = deepcopy(item["result"]["questions"])
            entry["previous_citation_locations"] = {
                citation["ref"]: {key: citation[key] for key in (
                    "source_id", "title", "url", "article_no", "publish_date", "effective_date", "kind",
                ) if key in citation}
                for citation in (item["result"] or {}).get("citations", [])
            }
        previous_materials = [m for m in conversation["materials"] if m["id"] in item["material_ids"]]
        passages.extend(p for p in material_passages(item.get("state", {}), previous_materials)
                        if p["material_version_id"] in selected)
        history.append(entry)
    return history, merge_material_passages(passages)
