"""Assess actual model-visible evidence, independently from generated answer quality."""

from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from law_agent.config import load_env_file, require_agent_llm_config
from law_agent.llm.openai_compatible import ChatMessage, OpenAICompatibleClient
from law_agent.llm.usage import usage_scope
from law_agent.review.retrieval.corpus import load_corpus
from scripts.eval_qa_retrieval_chain import FIXTURE, read, visible_texts, write

PROMPT = """你是独立的法律检索评审员，只评给定原文能否支撑原始用户问题，不评价模型答案，不补查，不向被评模型反馈。
逐项评价requirement_drafts内的核心证据需求。结合question、material和rubric确定实际需要支持的条件与例外；drafts未完成专业审定，若有歧义应说明。事实待核实不等于法源检索不足；已有条件规则就可支持有条件答复。用户intake是明确标注的用户陈述，不因未经第三方验证就忽略。
source_text_registry只是一张共享原文表。每个匿名方案首轮仅能使用initial_visible_ids，首次回答前仅能使用pre_answer_seen_ids；候选ID、目录预览、未读链接都不能算已取得完整证据。reference_only_not_retrieved仅供独立核验，不能计为任何方案已取得。
允许等价官方解释，不要求指定条号齐全；但应核对适用条件、版本、地域、主体、例外与法律效力。参考标准不得替代其并不具有的强制法律依据。用户未问的手续或模型可能额外扩写的义务不得增加为必需检索项。
若规则已取得但业务事实未知，应在fact_gaps列出而不扣规则覆盖。若题目只问机制免予边界，不要求取得每一项剩余义务完整原文。若用户询问准备文件和备案，则需核查与本案有关的剩余义务及其例外。历史时点按用户指定时间评价，不用后来的法源替代。
对每个requirement_id分别判initial_status和pre_answer_status为covered、partial、missing、unscorable。covered代表必要条件/例外均得到支持，而非标题或概念相关。sufficiency=sufficient仅当全部核心组covered；有部分支持但仍缺核心规则为partial；主要规则均缺为insufficient。
supporting_ids只能引用该方案pre_answer_seen_ids，initial_supporting_ids只能引用initial_visible_ids。每个组简要说明已有支持和具体缺口，不输出替代法律答复，不评价回答措辞或扩写。
返回简洁JSON：{"packages":[{"label":"A","initial_sufficiency":"sufficient|partial|insufficient|unscorable","pre_answer_sufficiency":"sufficient|partial|insufficient|unscorable","requirements":[{"requirement_id":"题目给定ID","initial_status":"covered|partial|missing|unscorable","pre_answer_status":"covered|partial|missing|unscorable","initial_supporting_ids":[],"supporting_ids":[],"missing_rule":"无缺口则为空","explanation":"具体条件的支持与缺口"}],"fact_gaps":[],"explanation":"简述"}]}。每个方案和每个需求ID均且仅出现一次。这是候选集筛查，不是正式通过率。"""


def compact(hit, text=None):
    return {k: hit.get(k) for k in ("chunk_id", "source_id", "title", "article_no", "can_cite_clause",
            "citation_role", "source_url", "publish_date", "effective_date", "law_status")} | {
        "text": text if text is not None else hit.get("full_article_text") or hit.get("text", "")}


def reference_texts(cid, corpus):
    flow = "cac_cross_border_data_flow_rules_2024"
    pipl = "flk_npc_ff8081817b6472a3017b656cc2040044"
    assessment = "cac_data_export_security_assessment_measures_2022"
    articles = {flow: {"第五条", "第七条", "第八条", "第十三条"}}
    if cid in {"eval_standard_contract_003", "qa_exemption_scope_001"}:
        articles = {flow: {"第五条", "第十条"}, pipl: {"第十三条", "第三十九条", "第五十五条", "第五十六条"}}
    elif cid == "eval_sensitive_002":
        articles = {pipl: {"第二十八条", "第二十九条", "第三十条"}}
    elif cid.startswith("agent_historical"):
        articles = {assessment: {"第二条", "第四条"}, flow: {"第十四条"}}
    elif cid == "eval_shanghai_001":
        articles = {flow: {"第六条"}}
    elif cid == "agent_important_data_001":
        articles = {flow: {"第二条", "第七条"}}
    result = [compact(h) for h in corpus.values() if h["source_id"] in articles and h.get("article_no") in articles[h["source_id"]]]
    if cid == "eval_shanghai_001":
        local = [h for h in corpus.values() if "上海" in h["title"] or "临港" in h["title"]]
        result.extend(compact(h) for h in local)
    return result


def audit_case(dest, corpus):
    received = read(dest / "received_evidence.json")
    research = read(dest / "research.json")
    candidates = read(dest / "candidate_windows.json")
    events = read(dest / "termination_attempts.json")
    run = read(dest / "conversation.json")["runs"][-1]
    registry, views, seen = {}, [], set()
    initial = None
    initial_sources = {}
    for path in sorted(dest.glob("model_*.request.json")):
        payload = read(path)
        local, sources, web = visible_texts(payload)
        for key, h in local.items():
            assert h["chunk_id"] in received
            text = h.get("full_article_text") or h.get("text", "")
            original = received[h["chunk_id"]]
            assert text and (text in original.get("text", "") or text in (original.get("full_article_text") or "")), key
            registry[key] = compact({**original, **sources.get(h["source_id"], {}), **h}, text=text)
        for key, h in web.items():
            registry[key] = {"id": key, "title": h.get("title"), "url": h["url"],
                             "citation_role": "web_discovery", "text": h["excerpt"]}
        ids = list(local) + list(web)
        if initial is None and ids:
            initial = ids[:]
            initial_sources = {k: registry[k] for k in ids}
        seen.update(ids)
        views.append({"request": path.name, "visible_ids": ids, "is_investigation": bool(payload.get("tools")),
                      "body_chars": sum(len(registry[key]["text"]) for key in ids)})
    first_event = next((e for e in events if e["type"] in {"direct_answer", "finish", "ask_user"}), None)
    if first_event:
        through = first_event["actual_model_requests"]
        pre = set().union(*(set(v["visible_ids"]) for v in views[:through])) if through else set()
    else:
        pre = seen
    candidate_ids = {key for c in candidates for key in c["candidate_ids"]}
    initial_candidate_ids = set(candidates[0]["candidate_ids"]) if candidates else set()
    cumulative = set()
    retained = True
    for view in views:
        if view["is_investigation"]:
            retained &= cumulative <= set(view["visible_ids"])
            cumulative = set(view["visible_ids"])
    def counts(ids):
        hits = [registry[i] for i in ids]
        return {"bodies": len(hits), "local_bodies": sum(not i.startswith("web:") for i in ids),
                "citable_articles": len({(h.get("source_id"), h.get("article_no")) for h in hits if h.get("can_cite_clause") and h.get("article_no")}),
                "sources": len({h.get("source_id") or h.get("url") for h in hits}),
                "body_chars": sum(len(h["text"]) for h in hits)}
    row = {"initial_visible_ids": initial or [], "pre_answer_seen_ids": sorted(pre),
           "final_visible_ids": views[-1]["visible_ids"] if views else [], "initial_counts": counts(initial or []),
           "pre_answer_counts": counts(pre), "candidate_ids": sorted(candidate_ids),
           "initial_candidate_ids": sorted(initial_candidate_ids), "views": views,
           "native_status": run["status"], "awaiting_user": bool((run.get("result") or {}).get("questions")),
           "history_retained": retained,
           "research": [{"kind": r["kind"], "queries": r.get("queries"), "arguments": r.get("arguments"), "selectors": r.get("selectors")} for r in research]}
    return row, registry


def validate(response, payload):
    errors = []
    expected = {p["label"]: p for p in payload["packages"]}
    groups = {r["requirement_id"] for r in payload["requirement_drafts"]}
    if len(response.get("packages", [])) != len(expected) or {p.get("label") for p in response.get("packages", [])} != set(expected):
        errors.append("package labels do not match")
    for package in response.get("packages", []):
        label = package.get("label")
        if label not in expected:
            continue
        requirements = package.get("requirements", [])
        if len(requirements) != len(groups) or {r.get("requirement_id") for r in requirements} != groups:
            errors.append(f"{label}: requirement IDs do not match")
        for req in requirements:
            for ids_key, allowed_key in [("initial_supporting_ids", "initial_visible_ids"), ("supporting_ids", "pre_answer_seen_ids")]:
                if set(req.get(ids_key, [])) - set(expected[label][allowed_key]):
                    errors.append(f"{label}:{req.get('requirement_id')}: unsupported {ids_key}")
            for stage, ids_key in [("initial", "initial_supporting_ids"), ("pre_answer", "supporting_ids")]:
                if req.get(stage + "_status") == "covered" and not req.get(ids_key):
                    errors.append(f"{label}:{req.get('requirement_id')}: covered without supporting text")
        for stage in ["initial", "pre_answer"]:
            if package.get(stage + "_sufficiency") == "sufficient" and any(r.get(stage + "_status") != "covered" for r in requirements):
                errors.append(f"{label}: inconsistent {stage} sufficiency")
    return errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--follow", action="store_true")
    args = parser.parse_args()
    source, out = args.source.resolve(), args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    fixture = {c["case_id"]: c for c in read(FIXTURE)["cases"] if c["entry"] == "qa"}
    selected = read(source / "manifest.json")["cases"]
    assert set(selected) <= set(fixture)
    fixture = {cid: fixture[cid] for cid in selected}
    corpus = {h.chunk_id: h.model_dump(mode="json") for h in load_corpus()}
    load_env_file()
    cfg = require_agent_llm_config()
    assert cfg.model == "deepseek-flash" and urlparse(cfg.base_url).hostname == "api.deepseek.com"
    cfg = replace(cfg, timeout_seconds=180, read_timeout_seconds=180, total_timeout_seconds=300)
    client = OpenAICompatibleClient(cfg)
    manifest = {"status": "running", "model": cfg.model, "source": str(source), "expected_cases": len(fixture),
                "scope": "core retrieval sufficiency only; generated answers excluded; no feedback to QA",
                "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
                "limit": "single evaluator; pending professional gold annotations; screening rather than formal accuracy"}
    write(out / "manifest.json", manifest)
    pending = list(fixture)
    results, audits, usage_all = [], [], []
    while pending:
        ready = [cid for cid in pending if (source / cid / "summary.json").exists()]
        if not ready:
            if args.follow and read(source / "manifest.json")["status"] == "running":
                time.sleep(5)
                continue
            break
        for cid in ready:
            pending.remove(cid)
            case = fixture[cid]
            row, registry = audit_case(source / cid, corpus)
            assert row["history_retained"], cid
            audits.append({"case_id": cid, **row})
            write(out / "trajectory_audit.json", audits)
            package = {"label": "A", **{k: row[k] for k in ("initial_visible_ids", "pre_answer_seen_ids", "native_status", "awaiting_user", "research")}}
            assembled = read(source / cid / "case.json")
            payload = {"question": assembled["question"], "material": assembled["material"], "rubric": case["case"]["rubric"],
                       "requirement_drafts": case["requirement_drafts"], "source_text_registry": registry,
                       "packages": [package], "reference_only_not_retrieved": reference_texts(cid, corpus)}
            assert cfg.api_key not in json.dumps(payload, ensure_ascii=False)
            dest = out / cid
            dest.mkdir(exist_ok=True)
            signature = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if (dest / "raw_result.json").exists():
                assert read(dest / "input_sha256.json") == signature
                response = read(dest / "raw_result.json")
                usage = read(dest / "usage.json")
                print("CACHED_REVIEW", cid, flush=True)
            else:
                write(dest / "input.json", payload)
                write(dest / "input_sha256.json", signature)
                usage = []
                print("EVALUATE", cid, flush=True)
                try:
                    with usage_scope("qa_core_retrieval_assessment", usage.append):
                        response = client.chat_json([ChatMessage(role="system", content=PROMPT),
                            ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False))], structured_output_mode="json_object", stream=True)
                except Exception as exc:
                    write(dest / "transport_failure.json", {"type": type(exc).__name__, "message": str(exc).replace(cfg.api_key, "[redacted]")})
                    print("REVIEW_TRANSPORT_FAILURE", cid, type(exc).__name__, flush=True)
                    write(dest / "usage.json", usage)
                    continue
                finally:
                    write(dest / "usage.json", usage)
                write(dest / "raw_result.json", response)
            errors = validate(response, payload)
            result = {"case_id": cid, "qualification_errors": errors, "review": response}
            results.append(result)
            usage_all.extend(usage)
            write(out / "summary.json", results)
            print("REVIEW_DONE", cid, [(p.get("initial_sufficiency"), p.get("pre_answer_sufficiency")) for p in response.get("packages", [])], "errors", len(errors), flush=True)
            manifest.update(completed_reviews=len(results))
            write(out / "manifest.json", manifest)
    manifest.update(status="completed" if len(results) == len(fixture) else "incomplete", pending_cases=pending,
                    completed_reviews=len(results), requests=len(usage_all),
                    prompt_tokens=sum(u.get("prompt_tokens") or 0 for u in usage_all),
                    completion_tokens=sum(u.get("completion_tokens") or 0 for u in usage_all),
                    qualification_errors=sum(len(r["qualification_errors"]) for r in results))
    write(out / "manifest.json", manifest)


if __name__ == "__main__":
    main()
