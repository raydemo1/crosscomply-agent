"""Production review runtime: one persistent compliance Agent per ReviewTask.

Invoked by the independent worker in ``review.worker``. The fixed pipeline in
``review.service`` serves only the retrieval benchmark, CLI and offline eval;
it is not a production entry point.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from law_agent.config import RerankMode
from law_agent.review.agent import AgentModel, AgentState, run_agent
from law_agent.review.agent_tools import ComplianceAgentTools
from law_agent.review.enterprise_store import MaterialVersion, ReviewTask
from law_agent.review.fact_provenance import confirmed_intake_ledger
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH
from law_agent.review.schemas import ReviewFacts


class AgentRuntimeStore(Protocol):
    def checkpoint_agent(
        self,
        task_id: str,
        *,
        state: dict[str, Any],
        expected_attempt: int | None = None,
    ) -> ReviewTask: ...

    def get_intake_snapshot(self, intake_snapshot_id: str): ...


def execute_agent_task(
    task: ReviewTask,
    *,
    store: AgentRuntimeStore,
    goal: str,
    material: str,
    material_versions: Sequence[MaterialVersion] = (),
    chunks_path: Path | str = DEFAULT_CHUNKS_PATH,
    rerank_mode: RerankMode = "off",
    on_web_findings: Any = None,
) -> AgentState:
    """Resume the saved state or start a new bounded Agent loop."""

    intake_snapshot = store.get_intake_snapshot(task.intake_snapshot_id)
    if intake_snapshot is None or intake_snapshot.case_id != task.case_id:
        raise RuntimeError("审查任务绑定的事实快照不存在")
    if intake_snapshot.material_snapshot_id != task.material_snapshot_id:
        raise RuntimeError("审查任务的事实快照与材料快照不匹配")
    state = (
        AgentState.model_validate(task.agent_state)
        if task.agent_state is not None
        else AgentState(
            goal=goal,
            facts=ReviewFacts.model_validate({
                key: value for key, value in intake_snapshot.intake.items()
                if key in ReviewFacts.model_fields
            }),
            fact_ledger=confirmed_intake_ledger(intake_snapshot.intake, intake_snapshot.id),
        )
    )
    tools = ComplianceAgentTools(
        chunks_path=chunks_path,
        question=goal,
        material_text=material,
        material_versions=material_versions,
        rerank_mode=rerank_mode,
        model_id=task.model_id,
    )
    claimed_attempt = task.attempt_count
    try:
        return run_agent(
            state,
            material=material,
            intake={"id": intake_snapshot.id, "facts": intake_snapshot.intake},
            material_snapshot_id=task.material_snapshot_id,
            decide=AgentModel(model_id=task.model_id),
            search=tools.search,
            read_evidence=tools.read_evidence,
            web_search=tools.search_web,
            on_web_findings=on_web_findings,
            finalize=lambda draft, current: tools.finalize(
                draft,
                current,
                case_id=task.case_id,
                intake_snapshot={"id": intake_snapshot.id, "facts": intake_snapshot.intake},
            ),
            abstain=lambda draft, current: tools.finalize(
                draft,
                current,
                case_id=task.case_id,
                intake_snapshot={"id": intake_snapshot.id, "facts": intake_snapshot.intake},
                system_abstention=True,
            ),
            checkpoint=lambda current: store.checkpoint_agent(
                task.id,
                state=current.model_dump(mode="json"),
                expected_attempt=claimed_attempt,
            ),
        )
    finally:
        tools.close()
