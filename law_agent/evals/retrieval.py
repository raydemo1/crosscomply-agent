"""Retrieval evaluation on the production search path.

Each scenario's fixed queries go to ``ComplianceAgentTools.search`` — the
same hybrid search, date filtering and soft boosts the Agent calls — so the
numbers describe the system users get. Query planning quality is a separate
question, answered by the Agent eval's search logs.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path
from statistics import mean
from typing import Any

from law_agent.data.schemas import StrictModel
from law_agent.evals.bank.schema import Provision, Scenario, render_materials
from law_agent.review.schemas import RetrievalHit

Search = Callable[[Scenario], list[RetrievalHit]]
K_VALUES = (5, 10)


def production_search(chunks_path: Path, *, rerank_mode: str = "off") -> Search:
    from law_agent.review.agent_tools import ComplianceAgentTools
    from law_agent.review.schemas import RetrievalQuery, ReviewFacts

    def search(scenario: Scenario) -> list[RetrievalHit]:
        tools = ComplianceAgentTools(chunks_path=chunks_path, question=scenario.question,
                                     material_text=render_materials(scenario), rerank_mode=rerank_mode)
        try:
            facts = ReviewFacts.model_validate({
                **{k: v for k, v in scenario.intake.items() if k in ReviewFacts.model_fields},
                "as_of_date": scenario.as_of_date,
            })
            queries = [RetrievalQuery(query_id=f"q{i + 1}", query_type="legal_issue", text=text)
                       for i, text in enumerate(scenario.retrieval.queries)]
            return tools.search(queries, facts)
        finally:
            tools.close()

    return search


def _matches(hit: RetrievalHit, provision: Provision) -> bool:
    return hit.source_id == provision.source_id and (
        provision.article_no is None or hit.article_no == provision.article_no
    )


class ScenarioRetrieval(StrictModel):
    scenario_id: str
    split: str
    judgment_points: list[str]
    answer_leak: bool
    hits: list[dict[str, Any]]
    must_cite_recall: dict[int, float | None]
    must_retrieve_recall: dict[int, float | None]
    excluded_hits: dict[int, list[str]]
    missed_must_cite: list[str]


def score_scenario(scenario: Scenario, hits: Sequence[RetrievalHit]) -> ScenarioRetrieval:
    labels = scenario.retrieval
    must_cite: dict[int, float | None] = {}
    must_retrieve: dict[int, float | None] = {}
    excluded: dict[int, list[str]] = {}
    for k in K_VALUES:
        top = list(hits[:k])
        must_cite[k] = (mean(any(_matches(h, p) for h in top) for p in labels.must_cite)
                        if labels.must_cite else None)
        must_retrieve[k] = (mean(any(_matches(h, p) for h in top) for p in labels.must_retrieve)
                            if labels.must_retrieve else None)
        excluded[k] = [f"{p.source_id} {p.article_no or ''}".strip() for p in labels.must_not_cite
                       if any(_matches(h, p) and h.can_cite_clause for h in top)]
    top = list(hits[:max(K_VALUES)])
    return ScenarioRetrieval(
        scenario_id=scenario.scenario_id, split=scenario.split, judgment_points=list(scenario.judgment_points),
        answer_leak=bool(scenario.answer_leak_sources),
        hits=[{"rank": i + 1, "source_id": h.source_id, "article_no": h.article_no,
               "can_cite_clause": h.can_cite_clause} for i, h in enumerate(top)],
        must_cite_recall=must_cite, must_retrieve_recall=must_retrieve, excluded_hits=excluded,
        missed_must_cite=[f"{p.source_id} {p.article_no}" for p in labels.must_cite
                          if not any(_matches(h, p) for h in top)],
    )


def aggregate(results: Sequence[ScenarioRetrieval]) -> dict[str, Any]:
    def summarize(rows: Sequence[ScenarioRetrieval]) -> dict[str, Any]:
        out: dict[str, Any] = {"n": len(rows)}
        for k in K_VALUES:
            cite = [r.must_cite_recall[k] for r in rows if r.must_cite_recall[k] is not None]
            retrieve = [r.must_retrieve_recall[k] for r in rows if r.must_retrieve_recall[k] is not None]
            out[f"must_cite_recall@{k}"] = mean(cite) if cite else None
            out[f"must_cite_scenarios@{k}"] = len(cite)
            out[f"must_retrieve_recall@{k}"] = mean(retrieve) if retrieve else None
            out[f"excluded_hit_scenarios@{k}"] = sum(bool(r.excluded_hits[k]) for r in rows)
        return out

    by_point: dict[str, list[ScenarioRetrieval]] = defaultdict(list)
    for row in results:
        for point in row.judgment_points:
            by_point[point].append(row)
    return {
        "overall": summarize(results),
        "by_split": {split: summarize([r for r in results if r.split == split])
                     for split in sorted({r.split for r in results})},
        "without_answer_leak": summarize([r for r in results if not r.answer_leak]),
        "by_judgment_point": {point: summarize(rows) for point, rows in sorted(by_point.items())},
    }


def run_retrieval(scenarios: Sequence[Scenario], search: Search) -> tuple[list[ScenarioRetrieval], dict[str, Any]]:
    results = [score_scenario(s, search(s)) for s in scenarios if s.retrieval is not None]
    return results, aggregate(results)
