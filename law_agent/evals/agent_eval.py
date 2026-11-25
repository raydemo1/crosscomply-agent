"""Run arms over scenarios with durable evidence and arm-blind grading."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from law_agent.data.io import read_manifest
from law_agent.data.schemas import SourceRecord
from law_agent.evals.arms.base import Arm, ArmContext
from law_agent.evals.bank.schema import Scenario
from law_agent.evals.delivery import Delivery
from law_agent.evals.grading import Grade, Judge, classify, deterministic_grade, resolve_citations
from law_agent.evals.runs import BLOCKING_HTTP, RunStore, infrastructure_http_status


def load_sources(chunks_path: Path) -> list[SourceRecord]:
    manifest = chunks_path.with_name("source_manifest.csv")
    return read_manifest(manifest) if manifest.exists() else []


def grade_delivery(scenario: Scenario, delivery: Delivery, sources: list[SourceRecord],
                   judge: Judge | None) -> tuple[Delivery, Grade]:
    by_id = {s.source_id: s for s in sources}
    delivery = delivery.model_copy(update={"citations": resolve_citations(delivery.citations, sources)})
    if delivery.status != "succeeded":
        return delivery, classify(scenario, delivery, None, None)
    deterministic = deterministic_grade(scenario, delivery, by_id)
    verdict = judge(scenario, delivery) if judge is not None and scenario.agent_rubric is not None else None
    return delivery, classify(scenario, delivery, deterministic, verdict)


def run_agent_eval(
    scenarios: Sequence[Scenario], arms: Sequence[Arm], *, store: RunStore, chunks_path: Path,
    judge: Judge | None, judge_model: str | None, workers: int = 1,
) -> list[Grade]:
    scenarios = [s for s in scenarios if s.agent_rubric is not None]
    store.write_manifest(kind="agent", scenarios=scenarios, chunks_path=chunks_path, config={
        "arms": {arm.name: arm.config() for arm in arms}, "judge_model": judge_model, "workers": workers,
    })
    sources = load_sources(chunks_path)
    blocked = threading.Event()
    lock = threading.Lock()
    grades: list[Grade] = []
    status: dict[str, Any] = {"total": len(scenarios) * len(arms), "completed": 0, "skipped_after_block": 0}

    def one(arm: Arm, scenario: Scenario) -> Grade | None:
        if blocked.is_set():
            return None
        directory = store.case_dir(arm.name, scenario.scenario_id)
        ctx = ArmContext(chunks_path=chunks_path, event=lambda kind, value: store.event(directory, kind, value))
        ctx.event("started", {"arm": arm.name})
        delivery = arm.run(scenario, ctx)
        delivery, grade = grade_delivery(scenario, delivery, sources, judge)
        store.save(directory, "delivery.json", delivery.model_dump(mode="json"))
        store.save(directory, "grade.json", grade.model_dump(mode="json"))
        message = delivery.error or (grade.judge.error if grade.judge else "") or ""
        if infrastructure_http_status(message) in BLOCKING_HTTP:
            blocked.set()
        return grade

    jobs = [(arm, scenario) for scenario in scenarios for arm in arms]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(one, arm, scenario): (arm, scenario) for arm, scenario in jobs}
        for future in as_completed(futures):
            grade = future.result()
            with lock:
                if grade is None:
                    status["skipped_after_block"] += 1
                else:
                    grades.append(grade)
                    status["completed"] += 1
                store.save(store.root, "status.json", status)
    store.finish({"kind": "agent", **status, "blocked": blocked.is_set()})
    return grades
