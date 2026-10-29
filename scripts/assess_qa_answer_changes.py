"""Blindly compare saved QA answers and evidence, without rerunning the baseline."""

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
from scripts.assess_qa_retrieval_chain import audit_case, reference_texts, validate
from scripts.eval_qa_retrieval_chain import FIXTURE, read, write

PROMPT = """你是独立的数据合规QA评审员。对每题两个匿名方案分开评检索和回答，只使用给定原文，不补查，不向被评模型反馈。
先按原始question、用户material、rubric和requirement_drafts评价核心证据充分性，不能因为某份答案扩写了额外义务而增加必需检索项。资料已足以支持条件性答复时，业务事实未知不等于法源不足。
每个package有自己的source_text_registry，首轮只用initial_visible_ids，首次回答前只用pre_answer_seen_ids。候选ID、目录预览、链接都不算已取得正文。reference_only_not_retrieved是独立核验资料，绝不能计为方案已召回。允许等价官方说明，不强制指定条号或必须另有解释问答。法条自身已经明确给出的条件或例外，无需额外解释资料才算覆盖。历史时点按用户指定时点评。
然后检查实际answer，区分：①无依据或超出原文支持强度的义务要求；②将未知事实当否定或确定事实；③错误套用规则或遗漏适用条件/例外；④前后矛盾；⑤引用条文/编号与主张不对应；⑥有支持但超出当前问题必要范围的扩写。这些分别记录，不能统称检索失败。
材料里明确提供的事实可作为归因清楚的前提，不因未第三方核实就无视。不要求固定输出格式、额外风险评估或逐项合规清单；但也不能因答案变短就放过缺少关键条件、例外或未回答核心问题。
无依据与不必要扩写分开：相关法源足够、合理且明确标注为建议的做法，不要求每个建议都有逐字法定条款；建议被写为法定义务应标出。仅把“必须”改成“可能”不能挽救无依据的确定性法律主张。引用标记存在不代表该原文支持引用句。
按整个答复及明确关联的上下文判断前提，不要求每句话重复条件；但不能替答复补出未表达的例外，也不能因中间提到例外就忽略清单、首段或末段的相反确定结论。
requirements逐项且仅一次，状态covered|partial|missing|unscorable；充分性sufficient|partial|insufficient|unscorable。sufficient只在全部核心组covered时使用。支持ID只能取该方案相应阶段已见ID。
claim_issues只列确有问题的主张，同一问题不重复计数。quote必须逐字摘自answer；kind选unsupported_obligation|unsupported_fact|wrong_application|internal_contradiction|citation_mismatch|unnecessary_expansion。severity选material|minor，给具体理由和支持核对的实际已见supporting_ids；若无相应已见依据可为空。仅有正确但无必要扩写不直接判核心法律错误。
返回JSON：{"packages":[{"label":"A","initial_sufficiency":"sufficient","pre_answer_sufficiency":"sufficient","requirements":[{"requirement_id":"给定ID","initial_status":"covered","pre_answer_status":"covered","initial_supporting_ids":[],"supporting_ids":[],"missing_rule":"","explanation":"简述"}],"core_answer_complete":true,"answer_use":"correct|qualified|incorrect|unscorable","claim_issues":[{"quote":"原句","kind":"unsupported_obligation","severity":"material","reason":"具体问题","supporting_ids":[]}],"explanation":"分清检索和答复"}]}。A和B均且仅一次。解释简明，不生成替代答复，不输出总体准确率。"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--follow", action="store_true")
    args = parser.parse_args()
    source, baseline, out = args.source.resolve(), args.baseline.resolve(), args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    source_manifest = read(source / "manifest.json")
    selected = source_manifest["cases"]
    fixture = {c["case_id"]: c for c in read(FIXTURE)["cases"] if c["entry"] == "qa"}
    assert set(selected) <= set(fixture)
    assert source_manifest["fixture_sha256"] == read(baseline / "manifest.json")["fixture_sha256"]
    corpus = {h.chunk_id: h.model_dump(mode="json") for h in load_corpus()}
    load_env_file()
    cfg = require_agent_llm_config()
    assert cfg.model == "deepseek-flash" and urlparse(cfg.base_url).hostname == "api.deepseek.com"
    cfg = replace(cfg, timeout_seconds=180, read_timeout_seconds=180, total_timeout_seconds=300)
    client = OpenAICompatibleClient(cfg)
    manifest = {"status": "running", "model": cfg.model, "cases": selected,
                "source": str(source), "baseline": str(baseline), "baseline_generation_repeated": False,
                "scope": "separate core evidence sufficiency and answer claim issues; no feedback to QA",
                "blinding": "labels alternate; no policy names, cost or answer lengths supplied",
                "prompt_sha256": hashlib.sha256(PROMPT.encode()).hexdigest(),
                "limit": "single evaluator, selected candidate cases, professional gold pending"}
    write(out / "manifest.json", manifest)
    pending = list(selected)
    results, usage_all = [], []
    while pending:
        ready = [cid for cid in pending if (source / cid / "summary.json").exists()]
        if not ready:
            if args.follow and read(source / "manifest.json")["status"] == "running":
                time.sleep(5)
                continue
            break
        for cid in ready:
            pending.remove(cid)
            index = selected.index(cid)
            paths = [("baseline", baseline / cid), ("candidate", source / cid)]
            if index % 2:
                paths.reverse()
            assert read(paths[0][1] / "case.json") == read(paths[1][1] / "case.json")
            packages, mapping, audits = [], {}, []
            for label, (name, path) in zip(["A", "B"], paths, strict=True):
                row, registry = audit_case(path, corpus)
                assert row["history_retained"]
                run = read(path / "conversation.json")["runs"][-1]
                mapping[label] = name
                audits.append({"label": label, "variant": name, **row})
                packages.append({"label": label, **{k: row[k] for k in ["initial_visible_ids", "pre_answer_seen_ids"]},
                                 "source_text_registry": registry, "answer": run.get("answer") or "",
                                 "answer_citations": (run.get("result") or {}).get("citations", [])})
            case = fixture[cid]
            assembled = read(source / cid / "case.json")
            payload = {"question": assembled["question"], "material": assembled["material"],
                       "rubric": case["case"]["rubric"], "requirement_drafts": case["requirement_drafts"],
                       "packages": packages, "reference_only_not_retrieved": reference_texts(cid, corpus)}
            assert cfg.api_key not in json.dumps(payload, ensure_ascii=False)
            dest = out / cid
            dest.mkdir(exist_ok=True)
            write(dest / "trajectory_audit.json", audits)
            signature = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            if (dest / "raw_result.json").exists():
                assert read(dest / "input_sha256.json") == signature
                response, usage = read(dest / "raw_result.json"), read(dest / "usage.json")
                print("CACHED_REVIEW", cid, flush=True)
            else:
                write(dest / "input.json", payload)
                write(dest / "input_sha256.json", signature)
                write(dest / "label_mapping.json", mapping)
                usage = []
                print("ASSESS_PAIR", cid, flush=True)
                try:
                    with usage_scope("qa_retrieval_and_claim_pair_assessment", usage.append):
                        response = client.chat_json([ChatMessage(role="system", content=PROMPT),
                            ChatMessage(role="user", content=json.dumps(payload, ensure_ascii=False))],
                            structured_output_mode="json_object", stream=True)
                finally:
                    write(dest / "usage.json", usage)
                write(dest / "raw_result.json", response)
            errors = validate(response, payload)
            for package in response.get("packages", []):
                original = next(p for p in packages if p["label"] == package["label"])
                for issue in package.get("claim_issues", []):
                    if not issue.get("quote") or issue["quote"] not in original["answer"]:
                        errors.append(f"{package['label']}: issue quote is not literal answer text")
                    if set(issue.get("supporting_ids", [])) - set(original["pre_answer_seen_ids"]):
                        errors.append(f"{package['label']}: issue references unseen evidence")
            row = {"case_id": cid, "label_mapping": mapping, "qualification_errors": errors, "review": response}
            results.append(row)
            usage_all.extend(usage)
            write(out / "summary.json", results)
            print("PAIR_DONE", cid, [(mapping[p["label"]], p.get("pre_answer_sufficiency"), p.get("answer_use"), len(p.get("claim_issues", []))) for p in response.get("packages", [])], "errors", len(errors), flush=True)
    manifest.update(status="completed" if len(results) == len(selected) else "incomplete", completed_reviews=len(results),
                    requests=len(usage_all), prompt_tokens=sum(u.get("prompt_tokens") or 0 for u in usage_all),
                    completion_tokens=sum(u.get("completion_tokens") or 0 for u in usage_all),
                    qualification_errors=sum(len(r["qualification_errors"]) for r in results))
    write(out / "manifest.json", manifest)


if __name__ == "__main__":
    main()
