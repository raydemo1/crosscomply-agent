"""Retrieval evaluation on the production search path.

Each scenario's fixed queries go to ``ComplianceAgentTools.search`` — the
same hybrid search, date filtering, soft boosts and evidence selection the
Agent calls — so the numbers describe what the Agent is handed.

Every labeled provision is scored at three levels of what the Agent receives:

* source — some body from the right source is returned (it knows which law to read);
* article — the labeled article itself is returned as a body (no extra read needed);
* discoverable — the article is acquired or listed with a preview in the search results (it can see it and read it).

A label also counts when one of its listed equivalent article groups is found
at that level. Query targets are scored on the labeled article itself.

Separately, each query is scored on the provisions it targets: the rank of the
target in that query's own fused ranking, before the batch shares out the
body slots. That splits "is the query/ranking good" from "is selection fair".
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from law_agent.data.schemas import StrictModel
from law_agent.evals.bank.schema import Provision, Scenario, render_materials
from law_agent.review.schemas import RetrievalHit

RESULT_DEPTH = 8  # matches agent_tools.RESULT_DEPTH
RANK_CUTOFFS = (3, RESULT_DEPTH)


@dataclass
class SearchOutcome:
    hits: list[RetrievalHit]
    results: list[dict[str, Any]] = field(default_factory=list)  # ComplianceAgentTools.search_results()
    per_query: list[list[RetrievalHit]] = field(default_factory=list)


Search = Callable[[Scenario], SearchOutcome]


def production_search(chunks_path: Path, *, rerank_mode: str | None = None) -> Search:
    from law_agent.review.agent_tools import ComplianceAgentTools
    from law_agent.review.schemas import RetrievalQuery, ReviewFacts

    def search(scenario: Scenario) -> SearchOutcome:
        tools = ComplianceAgentTools(chunks_path=chunks_path, question=scenario.question,
                                     material_text=render_materials(scenario), rerank_mode=rerank_mode)
        try:
            facts = ReviewFacts.model_validate({
                **{k: v for k, v in scenario.intake.items() if k in ReviewFacts.model_fields},
                "as_of_date": scenario.as_of_date,
            })
            queries = [RetrievalQuery(query_id=f"q{i + 1}", query_type="legal_issue", text=query.text)
                       for i, query in enumerate(scenario.retrieval.queries)]
            hits = tools.search(queries, facts)
            return SearchOutcome(hits=hits, results=tools.search_results(),
                                 per_query=tools.last_query_candidates())
        finally:
            tools.close()

    return search


def _matches(hit: RetrievalHit, provision: Provision) -> bool:
    return _in_hits([hit])(provision.source_id, provision.article_no)


def _in_hits(hits: Sequence[RetrievalHit]) -> Callable[[str, str | None], bool]:
    return lambda source_id, article_no: any(
        h.source_id == source_id and (article_no is None or h.article_no == article_no) for h in hits)


def _in_results(results: Sequence[dict[str, Any]]) -> Callable[[str, str | None], bool]:
    def present(source_id: str, article_no: str | None) -> bool:
        return any(group["source_id"] == source_id and (
            article_no is None or any(loc.get("article_no") == article_no for loc in group["locations"]))
            for group in results)

    return present


class LabelResult(StrictModel):
    kind: Literal["must_cite", "must_retrieve"]
    key: str
    source: bool
    article: bool
    discoverable: bool


class QueryTargetResult(StrictModel):
    query: str
    target: str
    rank: int | None  # 1-based in the query's own fused ranking; None = beyond the candidate depth


class ScenarioRetrieval(StrictModel):
    scenario_id: str
    split: str
    judgment_points: list[str]
    answer_leak: bool
    hits: list[dict[str, Any]]
    labels: list[LabelResult]
    query_targets: list[QueryTargetResult]
    excluded_hits: list[str]


def score_scenario(scenario: Scenario, outcome: SearchOutcome) -> ScenarioRetrieval:
    labels = scenario.retrieval
    hits = outcome.hits
    results: list[LabelResult] = []
    in_hits, in_nav = _in_hits(hits), _in_results(outcome.results)
    for kind in ("must_cite", "must_retrieve"):
        for p in getattr(labels, kind):
            results.append(LabelResult(
                kind=kind, key=p.key, source=p.satisfied(lambda source_id, _article: in_hits(source_id, None)),
                article=p.satisfied(in_hits),
                discoverable=p.satisfied(lambda s, a: in_hits(s, a) or in_nav(s, a)),
            ))
    provisions = {p.key: p for p in (*labels.must_cite, *labels.must_retrieve, *labels.optional)}
    targets: list[QueryTargetResult] = []
    for query, ranked in zip(labels.queries, outcome.per_query, strict=False):
        for key in query.targets:
            rank = next((i + 1 for i, h in enumerate(ranked) if _matches(h, provisions[key])), None)
            targets.append(QueryTargetResult(query=query.text, target=key, rank=rank))
    return ScenarioRetrieval(
        scenario_id=scenario.scenario_id, split=scenario.split, judgment_points=list(scenario.judgment_points),
        answer_leak=bool(scenario.answer_leak_sources),
        hits=[{"rank": i + 1, "source_id": h.source_id, "article_no": h.article_no,
               "can_cite_clause": h.can_cite_clause} for i, h in enumerate(hits)],
        labels=results, query_targets=targets,
        excluded_hits=[p.key for p in labels.must_not_cite if any(_matches(h, p) and h.can_cite_clause for h in hits)],
    )


def _rate(values: Sequence[bool]) -> float | None:
    return sum(values) / len(values) if values else None


def aggregate(results: Sequence[ScenarioRetrieval]) -> dict[str, Any]:
    """Rates pool every labeled provision (micro average): each clause counts once."""

    def summarize(rows: Sequence[ScenarioRetrieval]) -> dict[str, Any]:
        out: dict[str, Any] = {"n": len(rows)}
        for kind in ("must_cite", "must_retrieve"):
            items = [label for r in rows for label in r.labels if label.kind == kind]
            out[f"{kind}_n"] = len(items)
            for level in ("source", "article", "discoverable"):
                out[f"{kind}_{level}"] = _rate([getattr(label, level) for label in items])
        ranks = [t.rank for r in rows for t in r.query_targets]
        out["query_targets_n"] = len(ranks)
        for cutoff in RANK_CUTOFFS:
            out[f"query_target@{cutoff}"] = _rate([rank is not None and rank <= cutoff for rank in ranks])
        out["query_target_found"] = _rate([rank is not None for rank in ranks])
        out["excluded_hit_scenarios"] = sum(bool(r.excluded_hits) for r in rows)
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
