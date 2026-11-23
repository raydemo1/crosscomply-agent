"""Q&A Agent evaluation: what the Agent gathers and cites after its own multi-round research.

The retrieval eval scores one batch of fixed queries. Here the production Q&A
Agent (``execute_qa``) plans its own queries, reads listed results and
articles, and answers; ``evals.evidence`` scores what it gathered and cited
against the same article-level labels (the review-report Agent uses the same scorer).

Nothing here calls the semantic verifier; the Q&A path has none. "Today" is
pinned to the scenario's ``as_of_date``. When the Agent asks the applicant a
question, the scenario's scripted answers are replayed as the next turns.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from statistics import median
from typing import Any, Literal, get_args

from pydantic import Field

from law_agent.data.schemas import StrictModel
from law_agent.evals.bank.schema import Scenario
from law_agent.evals.evidence import EvidenceScore, score_evidence, summarize_evidence

Executor = Callable[..., None]


ReadOrigin = Literal["preview", "later_provision", "returned_source", "continuation", "direct"]


class ReadRecord(StrictModel):
    """One evidence read and where the Agent found what it asked for.

    preview — the location was listed with a preview in an earlier search's results;
    later_provision — it is a later provision shown with an earlier source;
    returned_source — another part of a source already returned as a body;
    continuation — paging or expanding an outline the Agent had opened;
    direct — none of the above: the Agent named the source itself.
    """

    turn: int
    source_id: str | None
    source_title: str | None
    article_no: str | None
    chunk_id: str | None
    origin: ReadOrigin
    ok: bool


class QARun(StrictModel):
    scenario_id: str
    split: str
    judgment_points: list[str]
    answer_leak: bool
    status: str  # completed | asked_user | failed
    error: str | None = None
    turns: int
    searches: int
    reads: int
    web_searches: int
    queries: list[str]
    answer: str
    citations: list[dict[str, Any]]
    evidence_refs: list[dict[str, Any]]  # what was gathered, so a label change can be rescored without a rerun
    read_records: list[ReadRecord] = Field(default_factory=list)
    evidence: EvidenceScore


def read_records(state: dict[str, Any], *, turn: int) -> list[ReadRecord]:
    """Classify every read in one turn's saved state by what the Agent had been shown before it."""

    hits = {hit["chunk_id"]: hit for hit in state.get("evidence", [])}
    listed_locations: set[tuple[str, str | None]] = set()
    listed_chunks: set[str] = set()
    listed_sources: set[str] = set()
    returned_sources: set[str] = set()
    later: set[tuple[str, str | None]] = set()

    def acquired(chunk_ids: list[str]) -> None:
        for chunk_id in chunk_ids:
            hit = hits.get(chunk_id)
            if hit is not None:
                returned_sources.add(hit["source_id"])
                later.update((p["source_id"], p.get("article_no")) for p in hit.get("later_provisions", []))

    records: list[ReadRecord] = []
    for step in state.get("steps", []):
        observation = step.get("observation", {})
        if step.get("action") == "search_evidence":
            acquired(observation.get("chunk_ids", []))
            for source in observation.get("results", []):
                listed_sources.add(source["source_id"])
                for location in source.get("locations", []):
                    if not location.get("acquired"):
                        listed_chunks.add(location["chunk_id"])
                        listed_locations.add((source["source_id"], location.get("article_no")))
        for result in observation.get("read_results", []) if step.get("action") == "read_evidence" else []:
            request = result.get("read_request", {})
            source = result.get("resolved_source_id") or request.get("source_id")
            article, chunk = request.get("article_no"), request.get("chunk_id")
            if request.get("offset") or request.get("node_id") or request.get("parent_node_id"):
                origin = "continuation"
            elif chunk in listed_chunks or (article and (source, article) in listed_locations) or (
                    not article and not chunk and source in listed_sources):
                origin = "preview"
            elif (source, article) in later:
                origin = "later_provision"
            elif source in returned_sources:
                origin = "returned_source"
            else:
                origin = "direct"
            records.append(ReadRecord(turn=turn, source_id=source, source_title=request.get("source_title"),
                                      article_no=article, chunk_id=chunk, origin=origin, ok="error" not in result))
            acquired(result.get("chunk_ids", []))
    return records


def score_run(scenario: Scenario, *, evidence: Sequence[dict[str, Any]], citations: Sequence[dict[str, Any]],
              **fields: Any) -> QARun:
    return QARun(
        scenario_id=scenario.scenario_id, split=scenario.split, judgment_points=list(scenario.judgment_points),
        answer_leak=bool(scenario.answer_leak_sources), citations=list(citations),
        evidence_refs=[{"chunk_id": e.get("chunk_id"), "source_id": e.get("source_id"),
                        "article_no": e.get("article_no")} for e in evidence],
        evidence=score_evidence(scenario.retrieval, evidence=evidence, citations=citations), **fields,
    )


def run_scenario(scenario: Scenario, *, chunks_path: Path, execute: Executor | None = None) -> QARun:
    from law_agent.evals.arms.crosscomply import parsed_files
    from law_agent.review.qa_agent import execute_qa

    execute = execute or execute_qa
    with tempfile.TemporaryDirectory() as tmp:
        files = parsed_files(scenario, Path(tmp))
    materials = [{"id": f"m{i + 1}", "filename": name, "parsed_text": text} for i, (name, text) in enumerate(files)]
    conversation: dict[str, Any] = {"id": scenario.scenario_id, "materials": materials, "runs": []}
    turns = [scenario.question, *(answer.answer for answer in scenario.scripted_answers)]
    evidence: dict[str, dict[str, Any]] = {}
    totals = {"turns": 0, "searches": 0, "reads": 0, "web_searches": 0}
    queries: list[str] = []
    reads: list[ReadRecord] = []
    status, error, answer, citations = "failed", None, "", []
    for index, question in enumerate(turns):
        run: dict[str, Any] = {"id": f"r{index + 1}", "question": question, "attempt": 1,
                               "material_ids": [m["id"] for m in materials], "status": "running",
                               "answer": "", "result": None, "state": {}}
        conversation["runs"].append(run)

        def checkpoint(*, _run: dict[str, Any] = run, **changes: Any) -> None:
            for key in ("state", "answer", "result"):
                if key in changes and changes[key] is not None:
                    _run[key] = changes[key]

        failure = None
        try:
            execute(conversation, run, checkpoint=checkpoint, chunks_path=chunks_path,
                    current_date=scenario.as_of_date)
        except Exception as exc:  # noqa: BLE001 - one failed scenario must not stop the run
            references = getattr(exc, "references", None)
            failure = f"{type(exc).__name__}: {exc}" + (f" {references}" if references else "")
        state = run["state"] or {}  # kept on failure too, so the gathered evidence still counts
        for hit in state.get("evidence", []):
            evidence.setdefault(hit["chunk_id"], hit)
        for key in totals:
            totals[key] += state.get(key, 0)
        queries += [q["text"] for q in state.get("queries", [])]
        reads += read_records(state, turn=index + 1)
        if failure is not None:
            status, error = "failed", failure[:1000]
            break
        result = run["result"] or {}
        run["status"] = "completed"
        if result.get("questions") and index + 1 < len(turns):
            continue
        status = "asked_user" if result.get("questions") else "completed"
        answer, citations = result.get("answer") or run["answer"], result.get("citations", [])
        break
    return score_run(scenario, evidence=list(evidence.values()), citations=citations, status=status, error=error,
                     answer=answer, queries=queries, read_records=reads, **totals)


def rescore(run: QARun, scenario: Scenario) -> QARun:
    """Score a saved run against the scenario's current labels, without calling the Agent again."""

    return run.model_copy(update={"evidence": score_evidence(
        scenario.retrieval, evidence=run.evidence_refs, citations=run.citations)})


def aggregate(runs: Sequence[QARun]) -> dict[str, Any]:
    def summarize(rows: Sequence[QARun]) -> dict[str, Any]:
        scored = [r for r in rows if r.status != "failed"]
        out: dict[str, Any] = {"n": len(rows), "completed": sum(r.status == "completed" for r in rows),
                               "asked_user": sum(r.status == "asked_user" for r in rows),
                               "failed": len(rows) - len(scored),
                               **summarize_evidence(r.evidence for r in scored)}
        for key in ("searches", "reads", "turns"):
            out[f"median_{key}"] = median([getattr(r, key) for r in scored]) if scored else None
        records = [record for r in scored for record in r.read_records]
        out["read_origins"] = {origin: sum(record.origin == origin for record in records)
                               for origin in get_args(ReadOrigin)}
        return out

    return {
        "overall": summarize(runs),
        "by_split": {split: summarize([r for r in runs if r.split == split]) for split in sorted({r.split for r in runs})},
        "without_answer_leak": summarize([r for r in runs if not r.answer_leak]),
    }


def run_qa_eval(scenarios: Sequence[Scenario], *, chunks_path: Path, workers: int = 4,
                execute: Executor | None = None, on_result: Callable[[QARun], None] | None = None,
                ) -> tuple[list[QARun], dict[str, Any]]:
    labeled = [s for s in scenarios if s.retrieval is not None]

    def one(scenario: Scenario) -> QARun:
        result = run_scenario(scenario, chunks_path=chunks_path, execute=execute)
        if on_result is not None:
            on_result(result)
        return result

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        runs = list(pool.map(one, labeled))
    return runs, aggregate(runs)


def qa_report(aggregate_: dict[str, Any], manifest: dict[str, Any]) -> str:
    def fmt(values: dict[str, Any], key: str) -> str:
        value = values.get(key)
        return "—" if value is None else f"{value:.1%}"

    def row(name: str, v: dict[str, Any]) -> str:
        return (f"| {name} | {v['n']} | {v['completed']}/{v['asked_user']}/{v['failed']} | {v['must_cite_n']} | "
                f"{fmt(v, 'must_cite_retrieved')} | {fmt(v, 'must_cite_cited')} | {fmt(v, 'must_retrieve_retrieved')} | "
                f"{v['excluded_citation_runs']} | {v['median_searches']} / {v['median_reads']} |")

    header = ["| 范围 | 场景 | 完成/追问/失败 | 必引条款 | 证据召回 | 回答引用 | 必检来源召回 | 引用排除条款的场景 | 检索/精读次数中位数 |",
              "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    lines = ["# 问答 Agent 评测报告", "", f"代码：{manifest.get('base_commit', '')[:12]}", "",
             ("生产问答 Agent 自主规划查询、按检索结果精读并作答，不经过语义核验器；审查时点固定为场景的 as_of_date。"
              "证据召回：标签条款（或其列明的等价条款）出现在本次检索或精读拿到的全部证据中；回答引用：最终回答引用了该条或其等价条款。"), "",
             *header, row("全部", aggregate_["overall"]), row("不含直接答案场景", aggregate_["without_answer_leak"])]
    lines += [row(f"split={k}", v) for k, v in aggregate_["by_split"].items()]
    origins = aggregate_["overall"]["read_origins"]
    names = {"preview": "来自检索结果的预览", "later_provision": "来自新规关联条款", "returned_source": "已返回来源的其他部分",
             "continuation": "续读或展开目录", "direct": "Agent 自行指定"}
    lines += ["", "## 精读从哪里来", "", "| 来源 | 次数 |", "| --- | ---: |",
              *(f"| {names[k]} | {v} |" for k, v in origins.items())]
    return "\n".join(lines) + "\n"
