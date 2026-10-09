"""Paired, sequential comparison of production guidance and an experimental context variant."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import threading
from contextlib import ExitStack
from dataclasses import replace
from functools import partial
from pathlib import Path
from unittest.mock import patch

from run_agent_baseline import run_one, write_json, write_report

from law_agent.config import (
    load_service_config,
    require_agent_llm_config,
    require_llm_config,
    require_semantic_llm_config,
)
from law_agent.review import agent, agent_tools
from law_agent.review.evalset.agent_review import review_groups
from law_agent.review.evalset.agent_schemas import AgentCase
from law_agent.review.ids import utc_now_iso
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH
from law_agent.review.semantic_grounding import SemanticGroundingVerifier

B_AGENT_GUIDANCE = """围绕准备作出的关键法律断言，主动调查哪些依据可能推翻它：相关的一般规定、例外、适用范围或新旧规则衔接。检索不只寻找支持拟采用结论的条文，也可针对相反解释查证。按本题重要性决定需要调查的争点，不为穷尽所有法律问题无限搜索；未发现反证不等于已证明反证不存在。保留清楚的个案判断依据与尚未核验范围，自主选择搜索、阅读、追问或收口。"""
B_VERIFIER_GUIDANCE = """contrast_authorities是同一次审查实际返回、但未被claims引用的有限对照证据，不自动成为报告引用，也不是正确答案。检查其中是否有具体的一般依据、例外或衔接规定与拟断言的义务相矛盾。发现会改变判断的明确依据时，说明其内容和适用关系；缺少必要法源时指出需查证的具体法律争点，不凭模型记忆补造规则。对照资料不相关或不足以推翻结论时，不因其存在就拒绝报告，也不要求核验题目范围之外的一切问题。"""


def comparison_cases() -> list[AgentCase]:
    all_cases = {case.case_id: case for group in review_groups().values() for case in group}
    ids = ["eval_standard_contract_003", "agent_hr_exception_missing_001",
           "holdout_foreign_origin_001", "holdout_personal_booking_001", "agent_small_sensitive_001"]
    cases = [all_cases[case_id].model_copy(deep=True) for case_id in ids]
    hr = cases[0]
    assert hr.material_text.count("出境12万人") == 1
    contrast = hr.model_copy(deep=True, update={
        "case_id": "ab_hr_count_invariance_001",
        "material_text": hr.material_text.replace("出境12万人", "出境120万人"),
        "intake": {**hr.intake, "annual_non_sensitive_count": "1200000"},
        "source_case_id": hr.case_id,
        "selection_reason": "仅改变累计人数，其余已确认HR免予前提保持不变；AI复核的合成对照，不是独立人审留出。",
    })
    return [*cases, contrast]


def contrast_authorities(evidence, draft, chunks_by_id) -> list[dict]:
    cited_ids = {chunk_id for claim in draft.claims for chunk_id in claim.supporting_chunk_ids}
    cited_sources = {hit.source_id for hit in evidence if hit.chunk_id in cited_ids}
    candidates = sorted(
        (hit for hit in evidence if hit.chunk_id not in cited_ids and hit.can_cite_clause),
        key=lambda hit: (hit.source_id not in cited_sources, -hit.score, hit.rank),
    )
    selected, seen, characters = [], set(), 0
    for hit in candidates:
        key = (hit.source_id, hit.article_no or hit.chunk_id)
        text = hit.full_article_text or hit.text
        if key in seen or characters + len(text) > 12000:
            continue
        chunk = chunks_by_id.get(hit.chunk_id)
        selected.append({
            "chunk_id": hit.chunk_id, "source_id": hit.source_id, "title": hit.title,
            "article_text": text, "citation_role": hit.citation_role,
            "authority": hit.authority, "law_status": hit.law_status,
            "publish_date": hit.publish_date, "effective_date": hit.effective_date,
            "source_url": hit.source_url,
            "applicable_region": chunk.applicable_region if chunk else None,
            "applicable_subjects": chunk.applicable_subjects if chunk else [],
        })
        seen.add(key)
        characters += len(text)
        if len(selected) == 8:
            break
    return selected


class ComparisonVerifier(SemanticGroundingVerifier):
    def __init__(self, *, variant: str, directory: Path, **kwargs):
        super().__init__(**kwargs)
        self.variant, self.directory = variant, directory

    def __call__(self, **kwargs):
        references = contrast_authorities(
            kwargs["evidence"], kwargs["draft"], kwargs.get("chunks_by_id") or {},
        ) if self.variant == "B" else []
        original_run = self.node.run

        def run(messages):
            messages = list(messages)
            if self.variant == "B":
                messages[0] = replace(messages[0], content=messages[0].content + "\n" + B_VERIFIER_GUIDANCE)
                payload = json.loads(messages[1].content)
                payload["contrast_authorities"] = references
                messages[1] = replace(messages[1], content=json.dumps(payload, ensure_ascii=False))
            with (self.directory / "verifier_inputs.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"at": utc_now_iso(), "messages": [
                    {"role": item.role, "content": item.content} for item in messages
                ]}, ensure_ascii=False) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return original_run(messages)

        with patch.object(self.node, "run", run):
            return super().__call__(**kwargs)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    cases = comparison_cases()
    rng = random.Random(20261010)
    first_variants = ["A"] * 3 + ["B"] * 3
    rng.shuffle(first_variants)
    order = [(case, variant) for case, first in zip(cases, first_variants, strict=True)
             for variant in (first, "B" if first == "A" else "A")]
    if args.dry_run:
        print(json.dumps({"cases_per_variant": len(cases), "runs": len(order),
                          "order": [(case.case_id, variant) for case, variant in order],
                          "model_calls": 0}, ensure_ascii=False))
        return 0
    if args.output_dir is None:
        parser.error("--output-dir is required")
    config = require_llm_config()
    service = load_service_config()
    root = args.output_dir
    root.mkdir(exist_ok=False, parents=True)
    for variant in ("A", "B"):
        (root / variant).mkdir()
    production = sorted(p for p in Path("law_agent").rglob("*.py") if "evalset" not in p.parts)
    hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in production}
    manifest = {
        "started_at": utc_now_iso(), "base_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "working_tree_dirty": bool(subprocess.check_output(["git", "status", "--porcelain"], text=True).strip()),
        "agent_model": config.model, "judge_model": config.model,
        "reasoning_effort": {
            "agent": require_agent_llm_config().reasoning_effort,
            "semantic_verifier": require_semantic_llm_config().reasoning_effort,
            "judge": config.reasoning_effort,
        },
        "structured_output_mode": {
            "agent": "json_object", "semantic_verifier": "json_object",
            "judge": config.structured_output_mode,
        },
        "embedding_provider": service.embedding.provider, "embedding_model": service.embedding.model,
        "es_index": service.elasticsearch.index_name, "pg_table": service.postgres.table_name,
        "workers": 1,
        "judge_protocol": "final_delivery_cited_evidence_fact_ledger_rubric_v2",
        "production_sha256": hashes,
        "corpus_sha256": hashlib.sha256(DEFAULT_CHUNKS_PATH.read_bytes()).hexdigest(),
        "source_manifest_sha256": hashlib.sha256(DEFAULT_CHUNKS_PATH.with_name("source_manifest.csv").read_bytes()).hexdigest(),
        "evaluation_sha256": {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in [
            Path(__file__), Path("scripts/run_agent_baseline.py"), *Path("law_agent/review/evalset").glob("agent_*.py")
        ]},
        "A_agent_prompt": agent.SYSTEM_PROMPT,
        "B_agent_addition": B_AGENT_GUIDANCE, "B_verifier_addition": B_VERIFIER_GUIDANCE,
        "B_context_limit": {"articles": 8, "characters": 12000, "already_returned_only": True},
        "cases": [case.model_dump(mode="json") for case in cases],
        "order": [(case.case_id, variant) for case, variant in order],
        "boundary": "每案每组单次；同模型judge；合成候选、AI离线复核，非独立人审或显著性检验；B为评测隔离实验。",
    }
    write_json(root / "manifest.json", manifest)
    records = {"A": {}, "B": {}}
    write_json(root / "status.json", {"updated_at": utc_now_iso(), "total": len(order),
                                     "completed": 0, "results": records})
    blocked = threading.Event()
    for case, variant in order:
        directory = root / variant / case.case_id
        with ExitStack() as stack:
            if variant == "B":
                stack.enter_context(patch.object(agent, "SYSTEM_PROMPT", agent.SYSTEM_PROMPT + "\n" + B_AGENT_GUIDANCE))
            stack.enter_context(patch.object(agent_tools, "SemanticGroundingVerifier", partial(
                ComparisonVerifier, variant=variant, directory=directory,
            )))
            record = run_one(case, directory, model=config.model, judge_model=config.model, blocked=blocked)
        records[variant][case.case_id] = record
        write_json(root / "status.json", {"updated_at": utc_now_iso(), "total": len(order),
                                         "completed": sum(len(group) for group in records.values()), "results": records})
        write_report(root / variant, cases, records[variant])
        print(json.dumps({"variant": variant, "case_id": case.case_id,
                          "overall_pass": record["overall_pass"], "categories": record["failure_categories"]}, ensure_ascii=False), flush=True)
    changed = [str(p) for p in production if not p.exists() or hashlib.sha256(p.read_bytes()).hexdigest() != hashes[str(p)]]
    write_json(root / "summary.json", {"finished_at": utc_now_iso(), "production_changed_during_run": changed, "results": records})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
