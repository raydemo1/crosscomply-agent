"""Run the business QA fixture with bounded searches and native cumulative history."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
import time
import traceback
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from law_agent.config import require_agent_llm_config
from law_agent.llm.embeddings import build_embeddings_provider
from law_agent.llm.usage import UsageRequest
from law_agent.review import agent_tools, qa_agent
from law_agent.review.agent_tools import ComplianceAgentTools
from law_agent.review.qa_store import InMemoryQAStore
from law_agent.review.retrieval.adapters import ElasticsearchKeywordAdapter, PgVectorAdapter
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH
from law_agent.review.retrieval.service_backends import (
    ServiceAdapters, create_elasticsearch_client, create_postgres_connection,
    make_pgvector_search_fn,
)
from law_agent.review.tool_calling import AgentAnswer, ToolCallingAgent

FIXTURE = ROOT / "docs/fixtures/business_retrieval_20261027.json"
CAP = 10


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def source_hashes():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (ROOT / "law_agent").rglob("*.py")}


def read_only_adapters(config):
    es = create_elasticsearch_client(config)
    conn = create_postgres_connection(config)
    provider = build_embeddings_provider(config.embedding)
    return ServiceAdapters(
        ElasticsearchKeywordAdapter(client=es, index_name=config.elasticsearch.index_name),
        PgVectorAdapter(search_fn=make_pgvector_search_fn(conn, config.postgres.table_name),
                        embed_texts=provider.embed_texts),
        lambda: (es.close(), conn.close()),
    )


def visible_texts(payload):
    local, sources, web = {}, {}, {}

    def retain_version(collection, key, value, text):
        if key in collection:
            prior = collection[key]
            prior_text = prior.get("excerpt") or prior.get("full_article_text") or prior.get("text", "")
            if text != prior_text:
                key += "#text_sha256=" + hashlib.sha256(text.encode()).hexdigest()[:16]
        collection[key] = value

    def collect_web(value):
        if isinstance(value, dict):
            if value.get("url") and value.get("excerpt"):
                retain_version(web, "web:" + value["url"], value, value["excerpt"])
            for child in value.values():
                collect_web(child)
        elif isinstance(value, list):
            for child in value:
                collect_web(child)

    for message in payload["messages"]:
        if message["role"] not in {"user", "tool"}:
            continue
        try:
            value = json.loads(message.get("content") or "")
        except (ValueError, TypeError):
            continue
        if not isinstance(value, dict):
            continue
        state = value.get("state", {})
        sources.update(state.get("evidence_sources", {}))
        for hit in state.get("evidence", []):
            if hit.get("full_article_text") or hit.get("text"):
                retain_version(local, hit["chunk_id"], hit, hit.get("full_article_text") or hit["text"])
        collect_web(value)
    return local, sources, web


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--cases", nargs="+")
    args = parser.parse_args()
    cases = [c for c in read(FIXTURE)["cases"] if c["entry"] == "qa"]
    assert len(cases) == 11 and len({c["case_id"] for c in cases}) == 11
    if args.cases:
        selected = set(args.cases)
        assert len(selected) == len(args.cases) and selected <= {c["case_id"] for c in cases}
        cases = [c for c in cases if c["case_id"] in selected]
    if args.preflight:
        print("NATIVE_HISTORY; QA_CASES", len(cases), "SEARCH_CAP", CAP, "RERANK off; ZERO_PROVIDER_REQUESTS")
        return
    cfg = require_agent_llm_config()
    assert cfg.model == "DeepSeek-V4.1-Flash-1017"
    out = args.output.resolve()
    expected = {
        "cases": [c["case_id"] for c in cases], "model": cfg.model, "rerank_mode": "off",
        "cap_per_search": CAP, "cap_across_rounds": None,
        "history": "native incremental tool dialogue; no body stripping or message projection",
        "fixture_sha256": hashlib.sha256(FIXTURE.read_bytes()).hexdigest(),
        "corpus_sha256": hashlib.sha256(DEFAULT_CHUNKS_PATH.read_bytes()).hexdigest(),
        "source_hashes": source_hashes(),
        "legal_prompt_sha256": hashlib.sha256(qa_agent.PROMPT.encode()).hexdigest(),
        "candidate_window_per_query": 50, "selection": "production strongest-query fusion; complete units; model max_chunks up to 10",
        "ranking_overridden": False,
        "planning": "fresh autonomous planning for every case; no reused answers or target hints",
        "native_budgets": {"turns": 16, "searches": 5, "reads": 10, "web_searches": 5},
        "minimum_request_interval_seconds": 40, "index_writes": 0,
    }
    if args.resume:
        manifest = read(out / "manifest.json")
        assert all(manifest[k] == v for k, v in expected.items())
    else:
        out.mkdir(parents=True, exist_ok=False)
        manifest = {**expected, "status": "running"}
    write(out / "manifest.json", manifest)
    bases = {
        "init": ComplianceAgentTools.__init__, "search": ComplianceAgentTools.search,
        "read": ComplianceAgentTools.read_evidence, "web": ComplianceAgentTools.search_web,
        "fuse": agent_tools.rrf_fuse, "usage": UsageRequest.__init__,
        "decide": ToolCallingAgent.decide,
    }
    native_messages = ToolCallingAgent.messages
    last_request = None
    summaries = []
    for item in cases:
        cid = item["case_id"]
        dest = out / cid
        if (dest / "summary.json").exists():
            summaries.append(read(dest / "summary.json"))
            print("CACHED_NATIVE_RUN", cid, flush=True)
            continue
        dest.mkdir(exist_ok=False)
        material = item["case"]["material_text"]
        if item["case"].get("intake"):
            material += "\n补充业务信息（用户提供，未独立核实）：\n" + json.dumps(item["case"]["intake"], ensure_ascii=False)
        case = {"question": item["case"]["question"], "material": material}
        write(dest / "case.json", case)
        calls, research, decisions, events, candidates, snapshots = [], [], [], [], [], []
        active_raw, received, ever_visible = [], {}, set()
        previous_ids = set()
        store = InMemoryQAStore()
        conversation_id = store.create("business QA retrieval validation")["id"]
        if material:
            store.add_material(conversation_id, {"id": "material", "filename": "用户材料.txt", "parsed_text": material})
        store.enqueue(conversation_id, question=case["question"], material_ids=["material"] if material else [], request_id=cid)
        print("START", cid, flush=True)

        def init_tools(self, *a, **kw):
            kw.update(rerank_mode="off", top_k=CAP)
            bases["init"](self, *a, **kw)

        def fuse(*a, **kw):
            result = bases["fuse"](*a, **kw)
            active_raw.append(result)
            return result

        def record_hits(hits):
            received.update({h.chunk_id: h.model_dump(mode="json") for h in hits})
            write(dest / "received_evidence.json", received)

        def search(self, *a, **kw):
            active_raw.clear()
            result = bases["search"](self, *a, **kw)
            assert len(result) <= CAP
            candidates.append({
                "candidate_ids": sorted({h.chunk_id for batch in active_raw for h in batch}),
                "selected_ids": [h.chunk_id for h in result],
                "per_query_rankings": [[{"chunk_id": h.chunk_id, "score": h.score, "rank": h.rank}
                                        for h in batch] for batch in active_raw],
            })
            write(dest / "candidate_windows.json", candidates)
            record_hits(result)
            research.append({"kind": "search", "queries": [q.model_dump(mode="json") for q in a[0]],
                             "facts": a[1].model_dump(mode="json"), "scope": kw,
                             "hits": [h.model_dump(mode="json") for h in result], "navigation": self.search_navigation()})
            write(dest / "research.json", research)
            print("TOOL", cid, "search", len(result), flush=True)
            return result

        def read_evidence(self, *a, **kw):
            result = bases["read"](self, *a, **kw)
            record_hits(result.hits)
            research.append({"kind": "read_evidence", "arguments": list(a[:3]), "selectors": kw,
                             "result": result.model_dump(mode="json")})
            write(dest / "research.json", research)
            print("TOOL", cid, "read", len(result.hits), result.mode, flush=True)
            return result

        def search_web(self, *a, **kw):
            result = bases["web"](self, *a, **kw)
            for finding in result.findings:
                received["web:" + finding.url] = finding.model_dump(mode="json")
            write(dest / "received_evidence.json", received)
            research.append({"kind": "search_web", "queries": [q.model_dump(mode="json") for q in a[0]],
                             "result": result.model_dump(mode="json")})
            write(dest / "research.json", research)
            print("TOOL", cid, "web", len(result.findings), flush=True)
            return result

        def usage(self, payload):
            nonlocal last_request, previous_ids
            assert ToolCallingAgent.messages is native_messages
            assert cfg.api_key not in json.dumps(payload, ensure_ascii=False)
            if last_request is not None:
                wait = 40 - (time.monotonic() - last_request)
                if wait > 0:
                    time.sleep(wait)
            last_request = time.monotonic()
            bases["usage"](self, payload)
            number = len(calls) + 1
            local, sources, web = visible_texts(payload)
            ids = set(local) | set(web)
            if payload.get("tools"):
                assert previous_ids <= ids, "native investigation lost previously visible original text"
                previous_ids = ids
            ever_visible.update(ids)
            snap = {"request": number, "local_ids": list(local), "web_ids": list(web),
                    "body_chars": sum(len(h.get("full_article_text") or h.get("text", "")) for h in local.values()) + sum(len(h["excerpt"]) for h in web.values()),
                    "native_history": True}
            snapshots.append(snap)
            write(dest / f"model_{number:02d}.request.json", payload)
            write(dest / "window_snapshots.json", snapshots)
            record = {"number": number, "phase": self.phase, "visible": snap}
            calls.append(record)
            prior = self.sink

            def sink(value):
                record.update(value)
                write(dest / f"model_{number:02d}.usage.json", record)
                write(dest / "calls.json", calls)
                if prior:
                    prior(value)

            self.sink = sink
            print("MODEL", cid, number, self.phase, "visible", len(ids), flush=True)

        def decide(self, session, **kw):
            result = bases["decide"](self, session, **kw)
            value = {"action": "direct_answer", "content": result.content} if isinstance(result, AgentAnswer) else result.model_dump(mode="json")
            decisions.append(value)
            write(dest / "decisions.json", decisions)
            if isinstance(result, AgentAnswer) or result.action in {"finish", "ask_user"}:
                events.append({"type": value["action"], "decision": value, "actual_model_requests": len(calls),
                               "ever_visible_keys": sorted(ever_visible), "received_keys": sorted(received),
                               "research_count": len(research)})
                write(dest / "termination_attempts.json", events)
            return result

        def execute(*a, **kw):
            try:
                return qa_agent.execute_qa(*a, **kw)
            except Exception as exc:
                write(dest / "execution_error.json", {"type": type(exc).__name__, "message": str(exc).replace(cfg.api_key, "[redacted]"),
                      "traceback": traceback.format_exc().replace(cfg.api_key, "[redacted]")})
                raise

        with ExitStack() as stack:
            for target, name, replacement in [
                (agent_tools, "build_service_adapters", read_only_adapters),
                (ComplianceAgentTools, "__init__", init_tools), (agent_tools, "rrf_fuse", fuse),
                (ComplianceAgentTools, "search", search),
                (ComplianceAgentTools, "read_evidence", read_evidence), (ComplianceAgentTools, "search_web", search_web),
                (ToolCallingAgent, "decide", decide), (UsageRequest, "__init__", usage),
            ]:
                stack.enter_context(patch.object(target, name, replacement))
            qa_agent.QAWorker(store, chunks_path=DEFAULT_CHUNKS_PATH, execute=execute).run_once()
        conversation = store.get(conversation_id)
        write(dest / "conversation.json", conversation)
        for name, value in [("calls", calls), ("research", research), ("decisions", decisions),
                            ("termination_attempts", events), ("window_snapshots", snapshots), ("received_evidence", received),
                            ("candidate_windows", candidates)]:
            write(dest / f"{name}.json", value)
        run = conversation["runs"][-1]
        (dest / "answer.md").write_text(run.get("answer") or "", encoding="utf-8")
        row = {"case_id": cid, "status": run["status"], "answer_chars": len(run.get("answer") or ""),
               "awaiting_user": bool((run.get("result") or {}).get("questions")),
               "actual_model_requests": len(calls), "tool_counts": dict(Counter(r["kind"] for r in research)),
               "received_unique_bodies": len(received), "ever_visible_unique_bodies": len(ever_visible),
               "max_model_visible_bodies": max((len(s["local_ids"]) + len(s["web_ids"]) for s in snapshots), default=0),
               "prompt_tokens": sum(c.get("prompt_tokens") or 0 for c in calls),
               "completion_tokens": sum(c.get("completion_tokens") or 0 for c in calls)}
        write(dest / "summary.json", row)
        summaries.append(row)
        write(out / "summary.json", summaries)
        print("RESULT", json.dumps(row, ensure_ascii=False), flush=True)
    assert expected["source_hashes"] == source_hashes()
    manifest.update(status="completed", completed_runs=len(summaries), source_stable=True,
                    actual_model_requests=sum(r["actual_model_requests"] for r in summaries),
                    prompt_tokens=sum(r["prompt_tokens"] for r in summaries),
                    completion_tokens=sum(r["completion_tokens"] for r in summaries))
    write(out / "manifest.json", manifest)


if __name__ == "__main__":
    main()
