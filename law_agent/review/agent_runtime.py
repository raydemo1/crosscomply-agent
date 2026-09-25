"""Production composition for one persistent compliance Agent run."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from law_agent.config import RerankMode
from law_agent.review.agent import AgentModel, AgentState, run_agent
from law_agent.review.agent_tools import ComplianceAgentTools
from law_agent.review.enterprise_store import MaterialVersion, ReviewTask
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH
from law_agent.review.web_research import WebFinding


class AgentRuntimeStore(Protocol):
    def checkpoint_agent(
        self,
        task_id: str,
        *,
        state: dict[str, Any],
        expected_attempt: int | None = None,
    ) -> ReviewTask: ...

    def get_rule_snapshot(self, rule_snapshot_id: str): ...


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

    rule_snapshot = store.get_rule_snapshot(task.rule_snapshot_id)
    if rule_snapshot is None or rule_snapshot.case_id != task.case_id:
        raise RuntimeError("审查任务绑定的规则快照不存在")
    state = (
        AgentState.model_validate(task.agent_state)
        if task.agent_state is not None
        else AgentState(goal=goal)
    )
    tools = ComplianceAgentTools(
        chunks_path=chunks_path,
        question=goal,
        material_text=material,
        material_versions=material_versions,
        rerank_mode=rerank_mode,
    )
    claimed_attempt = task.attempt_count
    try:
        return run_agent(
            state,
            material=material,
            rule={
                "id": rule_snapshot.id,
                "ruleset_version": rule_snapshot.ruleset_version,
                "facts": rule_snapshot.facts,
                "determination": rule_snapshot.determination,
            },
            decide=AgentModel(model_id=task.model_id),
            search=tools.search,
            web_search=tools.search_web,
            on_web_findings=on_web_findings,
            finalize=lambda draft, current: tools.finalize(
                draft,
                current,
                case_id=task.case_id,
                rule_snapshot={
                    "id": rule_snapshot.id,
                    "ruleset_version": rule_snapshot.ruleset_version,
                    "facts": rule_snapshot.facts,
                    "determination": rule_snapshot.determination,
                },
            ),
            checkpoint=lambda current: store.checkpoint_agent(
                task.id,
                state=current.model_dump(mode="json"),
                expected_attempt=claimed_attempt,
            ),
        )
    finally:
        tools.close()
