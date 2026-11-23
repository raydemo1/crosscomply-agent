"""Replay the Agent's own queries through production search, without calling the Agent.

The retrieval eval scores hand-written fixed queries; the Agent writes its
queries differently (often naming a law and an article). This eval freezes the
queries a real Q&A run produced (``export_queries``) and replays them through
``ComplianceAgentTools.search`` (hybrid search, boosts, rerank), so a search
change can be compared in minutes for embedding and rerank cost only.

Each labeled provision gets its best rank across all of the scenario's queries,
in each query's own ranking. An equivalent group ranks at its worst member, and
counts only when every member is found.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from law_agent.data.schemas import StrictModel
from law_agent.evals.bank.schema import Provision, Scenario, render_materials
from law_agent.review.schemas import RetrievalHit

QUERY_SETS_DIR = Path(__file__).parent / "bank" / "data" / "agent_queries"
RANK_CUTOFFS = (3, 8, 12, 50)
BATCH = 4  # queries per search, as the Agent's search tool allows

# One ranking per query: scenario, query texts -> per query, its fused ranking.
Rank = Callable[[Scenario, list[str]], list[list[RetrievalHit]]]


class QuerySet(StrictModel):
    source_run: str
    base_commit: str
    queries: dict[str, list[str]]  # scenario_id -> the Agent's query texts, in order


def export_queries(run_dir: Path) -> QuerySet:
    """Freeze the queries of a saved Q&A run. Only dev runs: the test split is not used for tuning."""

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    queries: dict[str, list[str]] = {}
    for path in sorted((run_dir / "qa").glob("*/result.json")):
        run = json.loads(path.read_text(encoding="utf-8"))
        if run["split"] != "dev":
            raise ValueError(f"{run['scenario_id']}: 只导出 dev 场景的查询，test 集不用于调参")
        if run["queries"]:
            queries[run["scenario_id"]] = list(dict.fromkeys(run["queries"]))
    return QuerySet(source_run=run_dir.name, base_commit=manifest.get("base_commit", ""), queries=queries)


def production_rank(chunks_path: Path) -> Rank:
    from law_agent.review.agent_tools import ComplianceAgentTools
    from law_agent.review.schemas import RetrievalQuery, ReviewFacts

    def rank(scenario: Scenario, texts: list[str]) -> list[list[RetrievalHit]]:
        tools = ComplianceAgentTools(chunks_path=chunks_path, question=scenario.question,
                                     material_text=render_materials(scenario))
        try:
            facts = ReviewFacts.model_validate({
                **{k: v for k, v in scenario.intake.items() if k in ReviewFacts.model_fields},
                "as_of_date": scenario.as_of_date,
            })
            ranked: list[list[RetrievalHit]] = []
            for start in range(0, len(texts), BATCH):
                batch = [RetrievalQuery(query_id=f"q{start + i}", query_type="legal_issue", text=text)
                         for i, text in enumerate(texts[start:start + BATCH])]
                tools.search(batch, facts)
                ranked += tools.last_query_candidates()
            return ranked
        finally:
            tools.close()

    return rank


class LabelRank(StrictModel):
    kind: Literal["must_cite", "must_retrieve"]
    key: str
    rank: int | None  # best rank over the scenario's queries; None = not in any query's candidates
    query: str | None  # the query that ranked it best


class ScenarioReplay(StrictModel):
    scenario_id: str
    queries: list[str]
    labels: list[LabelRank]


def _best(ranked: Sequence[Sequence[RetrievalHit]], texts: Sequence[str], source_id: str,
          article_no: str | None) -> tuple[int, str] | None:
    best = None
    for text, hits in zip(texts, ranked, strict=True):
        rank = next((i + 1 for i, h in enumerate(hits)
                     if h.source_id == source_id and (article_no is None or h.article_no == article_no)), None)
        if rank is not None and (best is None or rank < best[0]):
            best = (rank, text)
    return best


def _label_rank(provision: Provision, ranked, texts) -> tuple[int, str] | None:
    options = [_best(ranked, texts, provision.source_id, provision.article_no)]
    for group in provision.equivalents:
        members = [_best(ranked, texts, ref.source_id, ref.article_no) for ref in group]
        if all(members):
            options.append(max(members, key=lambda m: m[0]))
    found = [option for option in options if option is not None]
    return min(found, key=lambda m: m[0]) if found else None


def replay_scenario(scenario: Scenario, texts: list[str], rank: Rank, *, attempts: int = 3) -> ScenarioReplay:
    for attempt in range(attempts):
        try:
            ranked = rank(scenario, texts)
            break
        except RuntimeError:  # a transient backend error (e.g. a truncated embedding response) must not end the run
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt)
    labels = []
    for kind in ("must_cite", "must_retrieve"):
        for provision in getattr(scenario.retrieval, kind):
            best = _label_rank(provision, ranked, texts)
            labels.append(LabelRank(kind=kind, key=provision.key, rank=best and best[0], query=best and best[1]))
    return ScenarioReplay(scenario_id=scenario.scenario_id, queries=texts, labels=labels)


def aggregate(results: Sequence[ScenarioReplay]) -> dict[str, Any]:
    out: dict[str, Any] = {"scenarios": len(results), "queries": sum(len(r.queries) for r in results)}
    for kind in ("must_cite", "must_retrieve"):
        ranks = [label.rank for r in results for label in r.labels if label.kind == kind]
        out[f"{kind}_n"] = len(ranks)
        for cutoff in RANK_CUTOFFS:
            out[f"{kind}@{cutoff}"] = (sum(r is not None and r <= cutoff for r in ranks) / len(ranks)) if ranks else None
    return out


def run_replay(scenarios: Sequence[Scenario], query_set: QuerySet, rank: Rank,
               ) -> tuple[list[ScenarioReplay], dict[str, Any]]:
    """Scenarios without frozen queries (or without labels) are skipped and counted in the report."""

    results = [replay_scenario(s, query_set.queries[s.scenario_id], rank)
               for s in scenarios if s.retrieval is not None and s.scenario_id in query_set.queries]
    return results, aggregate(results)


def replay_report(aggregate_: dict[str, Any], manifest: dict[str, Any], query_set: QuerySet) -> str:
    def row(kind: str, name: str) -> str:
        cells = " | ".join("—" if aggregate_[f"{kind}@{c}"] is None else f"{aggregate_[f'{kind}@{c}']:.1%}"
                           for c in RANK_CUTOFFS)
        return f"| {name} | {aggregate_[f'{kind}_n']} | {cells} |"

    return "\n".join([
        "# Agent 查询回放评测", "",
        f"代码：{manifest.get('base_commit', '')[:12]}；查询来自 {query_set.source_run}（{query_set.base_commit[:12]}）", "",
        ("把一次问答评测中 Agent 实际写出的查询冻结下来，原样送进生产检索（含重排），不调用 Agent。"
         "每个标签条款取它在本场景所有查询各自排序中的最好名次；等价组以组内最差名次计，且须全部命中。"), "",
        f"场景 {aggregate_['scenarios']}，查询 {aggregate_['queries']}。", "",
        "| 标签 | 条数 | " + " | ".join(f"前{c}名" for c in RANK_CUTOFFS) + " |",
        "| --- | ---: | " + " | ".join("---:" for _ in RANK_CUTOFFS) + " |",
        row("must_cite", "必引条款"), row("must_retrieve", "必检来源"),
    ]) + "\n"
