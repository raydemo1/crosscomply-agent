"""Mutation testing of the independent semantic verifier.

Take a report draft that a human accepted as correct, inject one controlled
error at a time (a wrong threshold, a dropped exception, an assumed fact),
and check that the verifier rejects the mutant and points at the right field
while still accepting the clean draft. Reports catch rate, localization rate
and false rejection rate.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, model_validator

from law_agent.data.schemas import StrictModel
from law_agent.evals.bank.loader import BANK_DIR

VERIFIER_DIR = BANK_DIR / "verifier"


class Mutation(StrictModel):
    mutation_id: str
    kind: Literal["threshold", "dropped_exception", "assumed_fact", "wrong_path", "overreach", "other"]
    description: str = Field(min_length=1)
    path: str = Field(description="草稿字段路径，用点分隔，如 risk_boundaries.0 或 claims.1.text")
    find: str | None = Field(default=None, description="在该字段文本中替换的原文；与 drop 互斥")
    replace: str | None = None
    drop: bool = Field(default=False, description="删除列表中的该项")
    expected_target: str = Field(description="核验结果应定位到的目标，如 risk_boundaries:0 或 claims:1")

    @model_validator(mode="after")
    def one_operation(self) -> Mutation:
        if self.drop == (self.find is not None) or (self.find is not None and self.replace is None):
            raise ValueError("drop 与 find/replace 二选一")
        return self


class VerifierCase(StrictModel):
    case_id: str
    scenario_id: str | None = None
    review_goal: str
    as_of_date: date
    confirmed_intake: dict[str, object] = Field(default_factory=dict)
    material: str
    evidence_chunk_ids: list[str] = Field(min_length=1)
    draft: dict[str, Any] = Field(description="人工确认无误的 LLMReviewResultDraft")
    mutations: list[Mutation] = Field(min_length=1)


def load_verifier_cases(directory: Path = VERIFIER_DIR) -> list[VerifierCase]:
    return [VerifierCase.model_validate_json(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]


def apply_mutation(draft: dict[str, Any], mutation: Mutation) -> dict[str, Any]:
    mutated = copy.deepcopy(draft)
    *parents, last = mutation.path.split(".")
    node: Any = mutated
    for part in parents:
        node = node[int(part)] if isinstance(node, list) else node[part]
    key: Any = int(last) if isinstance(node, list) else last
    if mutation.drop:
        if not isinstance(node, list):
            raise ValueError(f"{mutation.mutation_id}: drop 只能用于列表项")
        node.pop(key)
        return mutated
    text = node[key]
    if not isinstance(text, str) or mutation.find not in text:
        raise ValueError(f"{mutation.mutation_id}: 字段 {mutation.path} 中找不到要替换的原文")
    node[key] = text.replace(mutation.find, mutation.replace, 1)
    return mutated


# verify(case, draft) -> (status, located_targets)
Verify = Callable[[VerifierCase, dict[str, Any]], tuple[str, set[str]]]


def production_verify(chunks_path: Path, *, model_id: str) -> Verify:
    from law_agent.review.fact_provenance import confirmed_intake_ledger
    from law_agent.review.result_builder import LLMReviewResultDraft
    from law_agent.review.retrieval.corpus import load_corpus
    from law_agent.review.retrieval.neighbors import hit_from_chunk
    from law_agent.review.schemas import ReviewFacts
    from law_agent.review.semantic_grounding import SemanticGroundingVerifier

    chunks = {c.chunk_id: c for c in load_corpus(chunks_path)}
    verifier = SemanticGroundingVerifier(model_id=model_id)

    def verify(case: VerifierCase, draft: dict[str, Any]) -> tuple[str, set[str]]:
        missing = [i for i in case.evidence_chunk_ids if i not in chunks]
        if missing:
            raise ValueError(f"{case.case_id}: evidence chunks not in corpus: {missing}")
        verdict = verifier(
            review_goal=case.review_goal, draft=LLMReviewResultDraft.model_validate(draft),
            confirmed_intake=case.confirmed_intake,
            extracted_facts=ReviewFacts.model_validate(
                {k: v for k, v in case.confirmed_intake.items() if k in ReviewFacts.model_fields}),
            material=case.material,
            evidence=[hit_from_chunk(chunks[i], rank) for rank, i in enumerate(case.evidence_chunk_ids)],
            chunks_by_id=chunks, fact_ledger=confirmed_intake_ledger(case.confirmed_intake, "verifier_eval"),
            as_of_date=case.as_of_date,
        )
        located = {f"claims:{c.claim_index}" for c in verdict.claim_checks if c.status != "supported"}
        located |= {c.target for c in [*verdict.core_checks, *verdict.report_checks] if c.status != "supported"}
        return verdict.status, located

    return verify


def run_verifier_suite(cases: list[VerifierCase], verify: Verify,
                       on_result: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []

    def record(row: dict[str, Any]) -> None:
        rows.append(row)
        if on_result:
            on_result(row)

    for case in cases:
        try:
            status, _ = verify(case, case.draft)
            record({"case_id": case.case_id, "variant": "clean", "status": status,
                    "correct": status == "supported"})
        except Exception as exc:  # noqa: BLE001
            record({"case_id": case.case_id, "variant": "clean", "error": f"{type(exc).__name__}: {exc}"})
        for mutation in case.mutations:
            try:
                status, located = verify(case, apply_mutation(case.draft, mutation))
                record({"case_id": case.case_id, "variant": mutation.mutation_id, "kind": mutation.kind,
                        "status": status, "caught": status != "supported",
                        "located": mutation.expected_target in located, "located_targets": sorted(located)})
            except Exception as exc:  # noqa: BLE001
                record({"case_id": case.case_id, "variant": mutation.mutation_id, "kind": mutation.kind,
                        "error": f"{type(exc).__name__}: {exc}"})
    clean = [r for r in rows if r["variant"] == "clean" and "error" not in r]
    mutants = [r for r in rows if r["variant"] != "clean" and "error" not in r]
    by_kind: dict[str, dict[str, int]] = {}
    for row in mutants:
        bucket = by_kind.setdefault(row["kind"], {"n": 0, "caught": 0, "located": 0})
        bucket["n"] += 1
        bucket["caught"] += row["caught"]
        bucket["located"] += row["caught"] and row["located"]
    return {
        "clean_n": len(clean),
        "false_rejections": sum(not r["correct"] for r in clean),
        "mutants_n": len(mutants),
        "caught": sum(r["caught"] for r in mutants),
        "caught_and_located": sum(r["caught"] and r["located"] for r in mutants),
        "errors": sum("error" in r for r in rows),
        "by_kind": by_kind,
        "rows": rows,
    }


def dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)
