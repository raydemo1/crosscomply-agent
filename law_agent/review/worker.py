"""Independent worker for PostgreSQL-persisted review tasks."""

from __future__ import annotations

import os
import socket
import time
from collections.abc import Callable
from typing import Any, Protocol

from law_agent.review.enterprise_store import ReviewTask
from law_agent.review.llm import ReviewWorkflowFailed


class ReviewTaskQueue(Protocol):
    def claim_next_task(self, *, worker_id: str) -> ReviewTask | None: ...

    def complete_task(
        self, task_id: str, *, result: dict[str, Any], final_node: str
    ) -> ReviewTask: ...

    def fail_task(
        self,
        task_id: str,
        *,
        failed_node: str,
        error_category: str,
        error_message: str,
    ) -> ReviewTask: ...

    def get_material_snapshot(self, snapshot_id: str): ...

    def get_material_version(self, version_id: str): ...


class ReviewWorker:
    """Claim exactly one task at a time and persist every terminal attempt."""

    def __init__(
        self,
        *,
        queue: ReviewTaskQueue,
        worker_id: str,
        execute: Callable[[ReviewTask], dict[str, Any]],
        on_started: Callable[[ReviewTask], None] | None = None,
        on_succeeded: Callable[[ReviewTask], None] | None = None,
        on_failed: Callable[[ReviewTask], None] | None = None,
    ) -> None:
        self._queue = queue
        self._worker_id = worker_id
        self._execute = execute
        self._on_started = on_started or (lambda _task: None)
        self._on_succeeded = on_succeeded or (lambda _task: None)
        self._on_failed = on_failed or (lambda _task: None)

    def run_once(self) -> ReviewTask | None:
        task = self._queue.claim_next_task(worker_id=self._worker_id)
        if task is None:
            return None
        self._on_started(task)
        try:
            result = self._execute(task)
        except ReviewWorkflowFailed as exc:
            failed = self._queue.fail_task(
                task.id,
                failed_node=exc.failed_node,
                error_category=exc.reason,
                error_message=exc.message,
            )
            self._on_failed(failed)
            return failed
        except Exception as exc:  # noqa: BLE001 - worker must persist unexpected failures
            failed = self._queue.fail_task(
                task.id,
                failed_node=task.current_node or "worker",
                error_category=exc.__class__.__name__,
                error_message=str(exc),
            )
            self._on_failed(failed)
            return failed
        completed = self._queue.complete_task(task.id, result=result, final_node="completed")
        self._on_succeeded(completed)
        return completed


def main() -> None:
    """Run the production worker until the container is stopped."""

    from law_agent.config import load_service_config
    from law_agent.review.api import _run_review, create_app
    from law_agent.review.case_store import PostgresCaseStore
    from law_agent.review.enterprise_store import PostgresEnterpriseStore
    from law_agent.review.workflow import next_status_after_review

    config = load_service_config()
    case_store = PostgresCaseStore(config.postgres.dsn)
    queue = PostgresEnterpriseStore(config.postgres.dsn)
    app = create_app(case_store=case_store)
    case_store.initialize()

    def execute(task: ReviewTask) -> dict[str, Any]:
        case = case_store.get_case(task.case_id)
        if case is None:
            raise RuntimeError(f"审查任务引用的案件不存在：{task.case_id}")
        snapshot = queue.get_material_snapshot(task.material_snapshot_id)
        if snapshot is None or snapshot.case_id != task.case_id:
            raise RuntimeError("审查任务绑定的材料快照不存在")
        versions = [queue.get_material_version(item) for item in snapshot.version_ids]
        if any(item is None or not (item.parsed_text or "").strip() for item in versions):
            raise RuntimeError("材料快照中存在未完成解析的版本")
        frozen_material = "\n\n".join(
            f"【{item.logical_name} v{item.version_number}】\n{item.parsed_text}"
            for item in versions
            if item is not None
        )
        response = _run_review(
            app,
            {
                **case,
                "material_text": frozen_material,
                "material_source": f"material_snapshot:{snapshot.id}",
            },
        )
        return response.model_dump(mode="json")

    def actor(case: dict[str, Any]) -> str:
        return case.get("owner_id") or case["created_by"]

    def on_started(task: ReviewTask) -> None:
        case = case_store.get_case(task.case_id)
        if case is None:
            return
        if case["status"] != "review_running":
            case_store.update_case(task.case_id, status="review_running")
        case_store.add_event(
            task.case_id,
            actor(case),
            event_type="review_started",
            from_status=case["status"],
            to_status="review_running",
            payload={"task_id": task.id, "attempt": task.attempt_count},
        )

    def on_succeeded(task: ReviewTask) -> None:
        case = case_store.get_case(task.case_id)
        if case is None or task.result is None:
            return
        review_result = task.result.get("review_result") or {}
        missing = review_result.get("missing_information") or []
        final_status = next_status_after_review(has_missing_information=bool(missing))
        case_store.update_case(
            task.case_id,
            status=final_status,
            risk_level=review_result.get("risk_level"),
            trace_id=task.result.get("trace_id"),
            response_json=task.result,
        )
        case_store.add_event(
            task.case_id,
            actor(case),
            event_type="review_completed",
            from_status="review_running",
            to_status=final_status,
            payload={"task_id": task.id},
        )

    def on_failed(task: ReviewTask) -> None:
        case = case_store.get_case(task.case_id)
        if case is None:
            return
        case_store.update_case(task.case_id, status="run_failed")
        case_store.add_event(
            task.case_id,
            actor(case),
            event_type="review_failed",
            from_status="review_running",
            to_status="run_failed",
            payload={
                "task_id": task.id,
                "failed_node": task.current_node,
                "error_category": task.error_category,
            },
        )

    worker_id = os.getenv("CROSSCOMPLY_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}"
    poll_seconds = max(0.2, float(os.getenv("CROSSCOMPLY_WORKER_POLL_SECONDS", "2")))
    worker = ReviewWorker(
        queue=queue,
        worker_id=worker_id,
        execute=execute,
        on_started=on_started,
        on_succeeded=on_succeeded,
        on_failed=on_failed,
    )
    while True:
        if worker.run_once() is None:
            time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
