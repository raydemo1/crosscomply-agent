"""Offline trace attribution and frozen navigation replay; no service or LLM calls."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH, load_corpus


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_results(observation: dict) -> list[dict]:
    if "read_results" in observation:
        return observation["read_results"]
    return [observation] if "read_request" in observation else []


def history_report(corpus_path: Path, roots: list[Path]) -> dict:
    chunks = load_corpus(corpus_path)
    by_id = {c.chunk_id: c for c in chunks}
    records = []
    known_target_cases = {
        "eval_standard_contract_003", "agent_hr_exception_missing_001",
        "holdout_personal_booking_001", "ab_hr_count_invariance_001",
    }
    for root in roots:
        assessment_path = root / "offline_assessment.json"
        findings = read_json(assessment_path).get("findings", []) if assessment_path.exists() else []
        for state_path in sorted(root.glob("*/*/state.json")):
            state = read_json(state_path)
            case_dir = state_path.parent
            variant, case_id = case_dir.parent.name, case_dir.name
            target = [c for c in chunks if c.title == "中华人民共和国个人信息保护法"
                      and c.article_no == "第十三条"] if case_id in known_target_cases else []
            target_ids = {c.chunk_id for c in target}
            steps = state.get("steps", [])
            returned = set()
            discovered = set()
            incomplete = []
            target_returned_steps = []
            source_discovery_steps = []
            for step in steps:
                obs = step.get("observation", {})
                returned.update(obs.get("chunk_ids", []))
                discovered.update(c["source_id"] for c in obs.get("keyword_candidates", []))
                step_ids = set(obs.get("chunk_ids", []))
                for result in read_results(obs):
                    returned.update(result.get("chunk_ids", []))
                    step_ids.update(result.get("chunk_ids", []))
                    req = result.get("read_request", {})
                    if "error" not in result and result.get("next_offset") is not None:
                        later = [r for s in steps if s["number"] > step["number"]
                                 for r in read_results(s.get("observation", {}))]
                        expected = {**req, "offset": result["next_offset"]}
                        if not any(r.get("read_request") == expected and "error" not in r
                                   for r in later):
                            incomplete.append(req)
                if target_ids & step_ids:
                    target_returned_steps.append(step["number"])
                step_sources = {by_id[cid].source_id for cid in step_ids if cid in by_id}
                step_sources.update(c["source_id"] for c in obs.get("keyword_candidates", []))
                if step_sources & {c.source_id for c in target}:
                    source_discovery_steps.append(step["number"])
            discovered.update(by_id[cid].source_id for cid in returned if cid in by_id)
            mismatches = sorted(returned - by_id.keys())
            text_mismatches = [hit["chunk_id"] for hit in state.get("evidence", [])
                               if hit["chunk_id"] in by_id
                               and hit.get("text") != by_id[hit["chunk_id"]].text]
            verifier_ids = set()
            verifier_target_calls = []
            verifier_path = case_dir / "verifier_inputs.jsonl"
            if verifier_path.exists():
                for index, line in enumerate(verifier_path.read_text(encoding="utf-8").splitlines(), 1):
                    call = json.loads(line)
                    call_ids = set()
                    for message in call.get("messages", []):
                        if message.get("role") != "user":
                            continue
                        payload = json.loads(message["content"])
                        for key in ("cited_authorities", "contrast_authorities"):
                            call_ids.update(_chunk_ids(payload.get(key, [])))
                    verifier_ids.update(call_ids)
                    if call_ids & target_ids:
                        verifier_target_calls.append({"line": index, "at": call.get("at"),
                                                      "target_chunk_ids": sorted(call_ids & target_ids)})
            annotations = [f for f in findings if f.get("case_id") == case_id
                           and variant in f.get("variants", [])]
            if mismatches or text_mismatches:
                category = "version_mismatch"
            elif not target:
                category = "insufficient_record"
            elif not {c.source_id for c in target} <= discovered:
                category = "source_not_discovered"
            elif target_ids <= returned:
                category = "text_returned_with_reviewed_error" if annotations else "text_returned"
            elif any(r.get("source_id") in {c.source_id for c in target}
                     and (r.get("article_no") == "第十三条"
                          or r.get("chunk_id") in target_ids) for r in incomplete):
                category = "unfinished_target_read"
            else:
                category = "source_discovered_target_not_returned"
            records.append({
                "artifact_dir": case_dir.as_posix(), "variant": variant, "case_id": case_id,
                "state_status": state.get("status"),
                "state_sha256": file_hash(state_path),
                "verifier_sha256": file_hash(verifier_path) if verifier_path.exists() else None,
                "target_basis": "PIPL article 13; prior offline assessment" if target else None,
                "target_chunk_ids": sorted(target_ids), "discovered_sources": sorted(discovered),
                "returned_target_chunk_ids": sorted(target_ids & returned),
                "source_discovery_steps": source_discovery_steps,
                "target_returned_steps": target_returned_steps,
                "verifier_target_chunk_ids": sorted(target_ids & verifier_ids),
                "verifier_target_calls": verifier_target_calls,
                "unmatched_chunk_ids": mismatches, "text_mismatches": text_mismatches,
                "unfinished_reads": incomplete, "category": category,
                "prior_findings": annotations,
            })
    return {"corpus_sha256": file_hash(corpus_path), "records": records,
            "limits": "Article 13 targets only for previously reviewed consent issues; "
                      "other cases remain unattributed. Prior findings are AI review, not expert labels. "
                      "Returned text does not prove it survived every model context projection."}


def _chunk_ids(value: Any) -> set[str]:
    if isinstance(value, dict):
        result = {value["chunk_id"]} if isinstance(value.get("chunk_id"), str) else set()
        for child in value.values():
            result.update(_chunk_ids(child))
        return result
    if isinstance(value, list):
        return set().union(*(_chunk_ids(child) for child in value)) if value else set()
    return set()


def offline_tools(chunks):
    from law_agent.review.agent_tools import ComplianceAgentTools

    tools = ComplianceAgentTools.__new__(ComplianceAgentTools)
    tools._chunks = chunks
    tools._chunks_by_id = {c.chunk_id: c for c in chunks}
    tools._sources_with_articles = {c.source_id for c in chunks if c.article_no}
    tools._candidate_hits = {}
    return tools


def flat_read(tools, source_id, article_no, chunk_id, facts, offset=0):
    """Frozen pre-tree read behavior, used only as the offline comparison arm."""
    from law_agent.review.agent_tools import _is_requested_article
    from law_agent.review.retrieval.neighbors import hit_from_chunk
    from law_agent.review.retrieval.temporal import filter_hits_as_of

    chunks = sorted((c for c in tools._chunks if c.source_id == source_id),
                    key=lambda c: c.chunk_index)
    if not (article_no or chunk_id):
        available = filter_hits_as_of([hit_from_chunk(c, i) for i, c in enumerate(chunks)],
                                     tools._chunks_by_id, as_of=facts.as_of_date)
        locations = {}
        for hit in available:
            locations.setdefault(hit.article_no or hit.chunk_id, {
                "article_no": hit.article_no, "chunk_id": hit.chunk_id,
                "preview": hit.text[:180],
            })
        entries = list(locations.values())
        page = entries[offset:offset + 30]
        return {"hits": [], "outline": page,
                "next_offset": offset + len(page) if offset + len(page) < len(entries) else None,
                "previous_chunk_id": None, "following_chunk_id": None}
    next_offset = previous = following = None
    if chunk_id:
        anchor = next(i for i, c in enumerate(chunks) if c.chunk_id == chunk_id)
        selected = chunks[max(0, anchor - 1):max(0, anchor - 1) + 5]
        previous, following = selected[0].prev_chunk_id, selected[-1].next_chunk_id
    else:
        matched = [c for c in chunks if _is_requested_article(c.article_no, article_no)]
        selected = matched[offset:offset + 5]
        if offset + len(selected) < len(matched):
            next_offset = offset + len(selected)
    hits = filter_hits_as_of([hit_from_chunk(c, i) for i, c in enumerate(selected)],
                             tools._chunks_by_id, as_of=facts.as_of_date)
    base = max((hit.score for hit in tools._candidate_hits.values()), default=0.0)
    hits = [h.model_copy(update={"score": round(base + 1.0, 6),
                                "source_has_articles": source_id in tools._sources_with_articles})
            for h in hits]
    return {"hits": [h.model_dump(mode="json") for h in hits], "outline": [],
            "next_offset": next_offset, "previous_chunk_id": previous,
            "following_chunk_id": following}


def replay_task(tools, task, facts, *, hierarchical, budget):
    targets = set(task["target_chunk_ids"])
    returned, errors, calls = set(), [], []

    def invoke(**selectors):
        if len(calls) >= budget:
            return None
        request = {"source_id": task["source_id"], "article_no": None, "chunk_id": None,
                   "offset": 0, **selectors}
        try:
            if hierarchical:
                response = tools.read_evidence(**request, facts=facts).model_dump(mode="json")
            else:
                response = flat_read(tools, **request, facts=facts)
        except (ValueError, StopIteration) as exc:
            errors.append(str(exc))
            calls.append({"request": request, "error": str(exc), "returned_chars": 0})
            return None
        returned.update(h["chunk_id"] for h in response["hits"])
        calls.append({"request": request, "response": response,
                      "returned_chars": len(json.dumps(response, ensure_ascii=False, separators=(",", ":")))})
        return response

    if hierarchical:
        first = tools._chunks_by_id[task["target_chunk_ids"][0]]
        path = [p.strip() for p in first.heading_path if p.strip()]
        while path and path[0] == first.title.strip():
            path.pop(0)
        path = path[:path.index(task["heading"]) + 1]
        parent = None
        for title in path:
            offset = 0
            entry = None
            while True:
                response = invoke(**({"parent_node_id": parent} if parent else {}), offset=offset)
                if response is None:
                    break
                entry = next((e for e in response["outline"] if e["title"] == title), None)
                if entry or response["next_offset"] is None:
                    break
                offset = response["next_offset"]
            if entry is None:
                errors.append(f"未找到目录节点：{title}")
                break
            parent = entry["node_id"]
        else:
            offset = 0
            while True:
                response = invoke(node_id=parent, offset=offset)
                if response is None or response["next_offset"] is None:
                    break
                offset = response["next_offset"]
    else:
        entries = []
        offset = 0
        while not targets <= returned:
            response = invoke(offset=offset)
            if response is None:
                break
            entries.extend(response["outline"])
            while not targets <= returned:
                entry = next((e for e in entries if e["chunk_id"] in targets - returned), None)
                if not entry:
                    break
                selected = {"article_no": entry["article_no"]} if entry["article_no"] else {
                    "chunk_id": entry["chunk_id"]}
                read_offset = 0
                while True:
                    read = invoke(**selected, offset=read_offset)
                    if read is None or read["next_offset"] is None:
                        break
                    read_offset = read["next_offset"]
                if read is None:
                    break
            if response["next_offset"] is None or len(calls) >= budget:
                break
            offset = response["next_offset"]
    return {"task_id": task["id"], "arm": "tree" if hierarchical else "flat",
            "budget": budget, "complete": targets <= returned, "requests": len(calls),
            "returned_chars": sum(c["returned_chars"] for c in calls),
            "missing_target_chunk_ids": sorted(targets - returned),
            "extra_chunk_ids": sorted(returned - targets), "errors": errors, "calls": calls}


def comparison_report(corpus_path: Path, task_path: Path) -> dict:
    from law_agent.review.retrieval.neighbors import hit_from_chunk
    from law_agent.review.retrieval.temporal import filter_hits_as_of
    from law_agent.review.schemas import ReviewFacts
    from law_agent.review.source_navigation import SourceNavigation

    manifest = read_json(task_path)
    if manifest["corpus_sha256"] != file_hash(corpus_path):
        raise ValueError("语料与冻结任务不一致；不自动重写目标")
    chunks = load_corpus(corpus_path)
    tools = offline_tools(chunks)
    facts = ReviewFacts(as_of_date=manifest["as_of_date"])
    for task in manifest["tasks"]:
        for cid in task["target_chunk_ids"]:
            c = tools._chunks_by_id[cid]
            if hashlib.sha256(c.text.encode()).hexdigest() != task["target_sha256"][cid]:
                raise ValueError(f"目标原文已变化：{cid}")
    audit = []
    for sid in sorted({c.source_id for c in chunks}):
        source = [c for c in chunks if c.source_id == sid]
        eligible = filter_hits_as_of([hit_from_chunk(c, i) for i, c in enumerate(source)],
                                    tools._chunks_by_id, as_of=facts.as_of_date)
        available = [tools._chunks_by_id[h.chunk_id] for h in eligible]
        nav = SourceNavigation(source, available, facts.as_of_date)
        owners = [cid for n in nav.nodes.values() for cid in n.direct_chunk_ids]
        expected = {c.chunk_id for c in available}
        traversed = set()
        preview_hits = 0
        pending = [None]
        while pending:
            parent = pending.pop()
            if not available:
                break
            offset = 0
            while True:
                page = tools.read_evidence(sid, None, None, facts, offset, parent_node_id=parent)
                preview_hits += len(page.hits)
                for entry in page.outline:
                    traversed.add(entry.node_id)
                    if entry.child_count:
                        pending.append(entry.node_id)
                if page.next_offset is None:
                    break
                offset = page.next_offset
        section_errors = []
        for node_id, node in nav.nodes.items():
            actual = []
            offset = 0
            while True:
                page = tools.read_evidence(sid, None, None, facts, offset, node_id=node_id)
                actual.extend(h.chunk_id for h in page.hits)
                if page.next_offset is None:
                    break
                offset = page.next_offset
            if actual != [c.chunk_id for c in node.chunks]:
                section_errors.append(node_id)
        audit.append({"source_id": sid, "eligible_chunks": len(expected),
                      "owned_once": Counter(owners) == Counter({cid: 1 for cid in expected}),
                      "all_nodes_reachable": traversed == nav.nodes.keys(),
                      "preview_hits": preview_hits, "section_errors": section_errors})
    results = [replay_task(tools, task, facts, hierarchical=tree, budget=budget)
               for task in manifest["tasks"] for tree, budget in [(False, 8), (False, 10), (True, 10)]]
    return {"corpus_sha256": file_hash(corpus_path), "tasks_sha256": file_hash(task_path),
            "as_of_date": manifest["as_of_date"], "audit": audit, "results": results,
            "limits": "Scripted known-target navigation; no LLM, search, or legal accuracy evaluation. "
                      "Targets cover existing heading membership only. "
                      "returned_chars counts compact JSON Unicode characters, not tokens or billing."}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CHUNKS_PATH)
    parser.add_argument("--history-root", type=Path, action="append")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compare", type=Path, help="Frozen task manifest; run navigation comparison")
    args = parser.parse_args()
    roots = args.history_root or [Path("data/review_runs/agent_ab_20261010"),
                                 Path("data/review_runs/agent_ab_20261010_continuation")]
    report = (comparison_report(args.corpus, args.compare) if args.compare
              else history_report(args.corpus, roots))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Saved report to {args.output}")


if __name__ == "__main__":
    main()
