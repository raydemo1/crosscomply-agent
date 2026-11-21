"""CrossComply arm: the production runtime, assembled exactly like the worker.

Materials go through the real upload parser (XLSX included), facts and the
entity portfolio are frozen as snapshots, the Agent runs under its budgets,
citation gate and semantic verifier, and a pause is answered from the
scenario's scripted answers before the task resumes.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import patch

from law_agent.config import RerankMode
from law_agent.evals.arms.base import ArmContext
from law_agent.evals.bank.loader import materialize
from law_agent.evals.bank.schema import ControlledWebFixture, Scenario, ScriptedAnswer
from law_agent.evals.delivery import CitedSource, Delivery
from law_agent.evals.runs import infrastructure_http_status
from law_agent.review.agent import AgentState, answer_agent
from law_agent.review.agent_runtime import execute_agent_task
from law_agent.review.enterprise_store import InMemoryEnterpriseStore, ReviewTask
from law_agent.review.http.schemas import IntakePayload
from law_agent.review.llm import ReviewWorkflowFailed
from law_agent.review.material_search import format_frozen_material
from law_agent.review.materials import material_from_file
from law_agent.review.web_research import WebSearchResult, is_official_url

WORKER_ID = "evals"
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# execute(task, store) -> AgentState after one attempt. Tests inject a fake.
Executor = Callable[[ReviewTask, InMemoryEnterpriseStore], AgentState]


def enqueue_eval_task(
    store: InMemoryEnterpriseStore, *, case_id: str, files: list[tuple[str, str]],
    intake: dict[str, object], model_id: str, portfolio: dict[str, object] | None = None,
) -> ReviewTask:
    """Freeze parsed files, confirmed facts and the portfolio, then queue a task."""

    versions = []
    for filename, parsed_text in files:
        data = parsed_text.encode("utf-8")
        versions.append(store.create_material_version(
            case_id=case_id, logical_name=filename, filename=filename,
            content_type=XLSX_TYPE if filename.endswith(".xlsx") else "text/plain",
            object_key=f"evals/{case_id}/{filename}", sha256=hashlib.sha256(data).hexdigest(),
            byte_size=len(data), uploaded_by="evals", parse_status="ready",
            parser="openpyxl" if filename.endswith(".xlsx") else "plain", parser_version="evals",
            parsed_text=parsed_text,
        ))
    snapshot = store.create_material_snapshot(
        case_id=case_id, version_ids=[v.id for v in versions], created_by="evals",
    )
    confirmed = IntakePayload().model_dump(mode="json")
    confirmed.update(intake)
    intake_snapshot = store.create_intake_snapshot(
        case_id=case_id, material_snapshot_id=snapshot.id, intake=confirmed, created_by="evals",
    )
    frozen = store.create_portfolio_snapshot(case_id, portfolio) if portfolio is not None else None
    return store.enqueue_review_task(
        case_id=case_id, material_snapshot_id=snapshot.id, intake_snapshot_id=intake_snapshot.id,
        portfolio_snapshot_id=frozen.id if frozen else None, model_id=model_id,
        data_boundary_summary={"evaluation": "evals"},
    )


def parsed_files(scenario: Scenario, directory: Path) -> list[tuple[str, str]]:
    """Run every bundle file through the production upload parser."""

    return [(path.name, material_from_file(path).material_text) for path in materialize(scenario, directory)]


def prepare_controlled_corpus(fixture: ControlledWebFixture, chunks_path: Path, output: Path) -> None:
    if any(not is_official_url(r.url) for r in fixture.results):
        raise ValueError("controlled Web results must use trusted official URLs")
    rows = [json.loads(line) for line in chunks_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    missing = set(fixture.held_out_source_ids) - {row["source_id"] for row in rows}
    if missing:
        raise ValueError(f"held-out sources are absent from the original corpus: {sorted(missing)}")
    remaining = [row for row in rows if row["source_id"] not in fixture.held_out_source_ids]
    if not remaining:
        raise ValueError("controlled corpus cannot be empty")
    output.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in remaining), encoding="utf-8")


class FixedWebClient:
    def __init__(self, fixture: ControlledWebFixture):
        self.results = [WebSearchResult(**r.model_dump()) for r in fixture.results]

    def search(self, query: str, *, max_results: int) -> list[WebSearchResult]:
        return self.results[:max_results]


class FirstDraftVerifier:
    """Stand-in for the semantic verifier: accept the Agent's first report as is.

    Evaluation-only. The citation gate still runs; only the independent model
    check (and the revision loop it would trigger) is skipped.
    """

    def __init__(self, *, model_id: str | None = None, client=None) -> None:
        self.model_id = model_id

    def __call__(self, *, draft, **_kwargs):
        from law_agent.review.semantic_grounding import ClaimCheck, SemanticVerdict

        return SemanticVerdict(
            status="supported",
            claim_checks=[ClaimCheck(claim_index=i, status="supported", reason="评测跳过独立语义校验")
                          for i in range(len(draft.claims))],
            conclusion_reason="评测跳过独立语义校验，直接采用首次生成的报告",
        )


def production_executor(
    scenario: Scenario, *, chunks_path: Path, rerank_mode: RerankMode | None = None, workdir: Path | None = None,
    skip_verifier: bool = False,
) -> Executor:
    def execute(task: ReviewTask, store: InMemoryEnterpriseStore) -> AgentState:
        snapshot = store.get_material_snapshot(task.material_snapshot_id)
        versions = [v for v in (store.get_material_version(i) for i in snapshot.version_ids) if v]
        with ExitStack() as stack:
            effective = chunks_path
            if skip_verifier:
                stack.enter_context(patch("law_agent.review.agent_tools.SemanticGroundingVerifier", FirstDraftVerifier))
            if scenario.controlled_web:
                directory = workdir or Path(stack.enter_context(TemporaryDirectory()))
                effective = directory / "controlled_chunks.jsonl"
                prepare_controlled_corpus(scenario.controlled_web, chunks_path, effective)
                stack.enter_context(patch("law_agent.review.agent_tools.build_web_search_client",
                                          return_value=FixedWebClient(scenario.controlled_web)))
            return execute_agent_task(
                task, store=store, goal=scenario.question, material=format_frozen_material(versions),
                material_versions=versions, chunks_path=effective, rerank_mode=rerank_mode,
                on_web_findings=lambda findings: None,
            )

    return execute


def _take_answer(answers: list[ScriptedAnswer], gate_id: str | None) -> ScriptedAnswer | None:
    for answer in [*(a for a in answers if a.gate_id is not None and a.gate_id == gate_id),
                   *(a for a in answers if a.gate_id is None)]:
        answers.remove(answer)
        return answer
    return None


def counters(state: AgentState | None) -> dict[str, int]:
    """Counters from the latest, cumulative state only (resumes carry history)."""

    if state is None:
        return {}
    return {
        "turns": state.turns, "searches": state.searches, "reads": state.reads,
        "web_searches": state.web_searches, "enrichment_submissions": state.enrichment_submissions,
        "request_inputs": sum(step.action == "request_input" for step in state.steps),
        "budget_exhausted": int(any(step.action == "budget_abstention" for step in state.steps)),
    }


def delivery_from_result(scenario_id: str, payload: dict[str, Any], **extra: Any) -> Delivery:
    review = payload.get("review_result") or {}
    risk = review.get("risk_level")
    form = ("abstain" if risk == "insufficient_evidence"
            else "conditional" if review.get("missing_information") else "definite")
    return Delivery(
        arm="crosscomply", scenario_id=scenario_id, status="succeeded", judgment_form=form,
        risk_level=risk, legal_path=review.get("legal_path"),
        conclusion="\n\n".join(filter(None, [review.get("decision_summary"), review.get("conclusion")])),
        citations=[CitedSource(title=c.get("title") or "", article_no=c.get("article_no"),
                               source_id=c.get("source_id"), can_cite_clause=c.get("can_cite_clause"))
                   for c in review.get("citations") or []],
        missing_information=review.get("missing_information") or [],
        freshness_hold=bool(payload.get("freshness_hold")), raw=payload, **extra,
    )


class CrossComplyArm:
    name = "crosscomply"

    def __init__(self, *, model_id: str, rerank_mode: RerankMode | None = None, skip_verifier: bool = False,
                 executor_factory=None) -> None:
        self.model_id = model_id
        self.rerank_mode = rerank_mode
        self.skip_verifier = skip_verifier
        self.executor_factory = executor_factory or production_executor

    def config(self) -> dict[str, Any]:
        return {"model": self.model_id, "rerank_mode": self.rerank_mode,
                "semantic_verifier": "skipped_first_draft" if self.skip_verifier else "on"}

    def run(self, scenario: Scenario, ctx: ArmContext) -> Delivery:
        started = time.perf_counter()
        store = _RecordingStore(ctx)
        with TemporaryDirectory() as directory:
            files = parsed_files(scenario, Path(directory) / "materials")
            enqueue_eval_task(store, case_id=scenario.scenario_id, files=files, intake=scenario.intake,
                              model_id=self.model_id, portfolio=scenario.portfolio)
            execute = self.executor_factory(scenario, chunks_path=ctx.chunks_path, rerank_mode=self.rerank_mode,
                                            workdir=Path(directory), skip_verifier=self.skip_verifier)
            delivery = self._loop(scenario, store, execute)
        delivery.latency_ms = int((time.perf_counter() - started) * 1000)
        return delivery

    def _loop(self, scenario: Scenario, store: InMemoryEnterpriseStore, execute: Executor) -> Delivery:
        pending = list(scenario.scripted_answers)
        asked: list[str] = []
        used: list[str] = []
        state: AgentState | None = None

        def failed(status: str, message: str) -> Delivery:
            return Delivery(arm=self.name, scenario_id=scenario.scenario_id, status=status,
                            error=message[:2000], counters=counters(state),
                            questions_asked=asked, answers_used=used)

        for _ in range(len(scenario.scripted_answers) + 2):
            task = store.claim_next_task(worker_id=WORKER_ID)
            if task is None:
                return failed("failed", "no claimable review task")
            attempt = task.attempt_count
            try:
                state = execute(task, store)
            except Exception as exc:  # noqa: BLE001 - one scenario must not crash the run
                latest = store.get_task(task.id)
                if latest and latest.agent_state:
                    state = AgentState.model_validate(latest.agent_state)
                message = exc.message if isinstance(exc, ReviewWorkflowFailed) else f"{type(exc).__name__}: {exc}"
                return failed("blocked" if infrastructure_http_status(message) else "failed", message)
            if state.status == "waiting_input":
                store.pause_task(task.id, state=state.model_dump(mode="json"), expected_attempt=attempt)
                asked.append(state.pending_question or "")
                answer = _take_answer(pending, state.gate_id)
                if answer is None:
                    return failed("unanswered_gate", "Agent 提出的问题没有对应的预设回答")
                answered = answer_agent(state, gate_id=state.gate_id, answer=answer.answer,
                                        provenance=answer.provenance, intake_snapshot_id=task.intake_snapshot_id)
                store.resume_task(task.id, state=answered.model_dump(mode="json"))
                used.append(answer.answer)
                continue
            store.complete_task(task.id, result=state.result or {}, final_node="completed",
                                expected_attempt=attempt)
            break
        else:
            return failed("failed", "attempt loop exceeded scripted answers")
        if state is None or not state.result:
            return failed("failed", "Agent finished without a result payload")
        return delivery_from_result(scenario.scenario_id, state.result, counters=counters(state),
                                    questions_asked=asked, answers_used=used)


class _RecordingStore(InMemoryEnterpriseStore):
    """Mirror every checkpoint into the run's event log."""

    def __init__(self, ctx: ArmContext) -> None:
        super().__init__()
        self.ctx = ctx
        self.seen_steps = 0

    def checkpoint_agent(self, task_id, *, state, expected_attempt=None):
        task = super().checkpoint_agent(task_id, state=state, expected_attempt=expected_attempt)
        steps = state.get("steps", [])
        for step in steps[self.seen_steps:]:
            self.ctx.event("step", step)
        self.seen_steps = len(steps)
        self.ctx.event("checkpoint", {"turns": state.get("turns"), "status": state.get("status")})
        return task
