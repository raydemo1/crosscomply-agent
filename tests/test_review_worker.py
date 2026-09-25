"""Tests for the persistent review worker boundary."""
from law_agent.review.http.schemas import IntakePayload



from law_agent.review.agent import AgentState
from law_agent.review.enterprise_store import InMemoryEnterpriseStore
from law_agent.review.llm import ReviewWorkflowFailed
from law_agent.review.worker import ReviewWorker, completion_has_missing_information


def _queued_task(store: InMemoryEnterpriseStore):
    version = store.create_material_version(
        case_id="case_001",
        logical_name="dpa",
        filename="dpa.pdf",
        content_type="application/pdf",
        object_key="cases/case_001/dpa.pdf",
        sha256="a" * 64,
        byte_size=12,
        uploaded_by="user_001",
    )
    snapshot = store.create_material_snapshot(
        case_id="case_001", version_ids=[version.id], created_by="user_001"
    )
    rules = store.create_intake_snapshot(case_id="case_001", material_snapshot_id=snapshot.id, intake=IntakePayload().model_dump(mode="json"), created_by="user_test")
    return store.enqueue_review_task(
        case_id="case_001",
        material_snapshot_id=snapshot.id,
        intake_snapshot_id=rules.id,
        model_id="approved-model",
        data_boundary_summary={"deployment": "intranet"},
    )


def test_worker_claims_and_completes_one_task() -> None:
    store = InMemoryEnterpriseStore()
    queued = _queued_task(store)
    worker = ReviewWorker(
        queue=store,
        worker_id="worker-a",
        execute=lambda task: {"task_id": task.id, "conclusion": "需要标准合同"},
    )

    result = worker.run_once()

    assert result is not None
    completed = store.get_task(queued.id)
    assert completed.status == "succeeded"
    assert completed.result == {"task_id": queued.id, "conclusion": "需要标准合同"}
    assert completed.attempts[-1].status == "succeeded"


def test_worker_preserves_failed_node_and_category() -> None:
    store = InMemoryEnterpriseStore()
    queued = _queued_task(store)

    def fail(_task):
        raise ReviewWorkflowFailed(
            failed_node="legal_retrieval",
            reason="service_unavailable",
            message="Elasticsearch unavailable",
            attempts=3,
        )

    worker = ReviewWorker(queue=store, worker_id="worker-b", execute=fail)

    assert worker.run_once() is not None
    failed = store.get_task(queued.id)
    assert failed.status == "failed"
    assert failed.current_node == "legal_retrieval"
    assert failed.error_category == "service_unavailable"
    assert failed.attempts[-1].error_message == "Elasticsearch unavailable"


def test_worker_returns_none_when_queue_is_empty() -> None:
    worker = ReviewWorker(
        queue=InMemoryEnterpriseStore(), worker_id="worker-empty", execute=lambda task: {}
    )
    assert worker.run_once() is None


def test_stale_worker_cannot_overwrite_a_reclaimed_attempt() -> None:
    store = InMemoryEnterpriseStore()
    queued = _queued_task(store)

    def lose_lease(_task):
        store.tasks[queued.id].lease_expires_at = "2000-01-01T00:00:00+00:00"
        assert store.requeue_expired_tasks() == 1
        reclaimed = store.claim_next_task(worker_id="worker-new")
        assert reclaimed is not None
        assert reclaimed.attempt_count == 2
        return {"conclusion": "旧 Worker 的迟到结果"}

    stale_worker = ReviewWorker(
        queue=store,
        worker_id="worker-old",
        execute=lose_lease,
    )

    current = stale_worker.run_once()

    assert current is not None
    assert current.status == "running"
    assert current.attempt_count == 2
    assert current.result is None


def test_worker_persists_agent_pause_and_resume() -> None:
    store = InMemoryEnterpriseStore()
    queued = _queued_task(store)
    waiting_state = AgentState(
        goal="判断是否需要补充境外接收方信息",
        status="waiting_input",
        turns=1,
        pending_question="请确认境外接收方所在国家或地区",
        gate_id="input_1",
    )
    worker = ReviewWorker(
        queue=store,
        worker_id="worker-agent",
        execute=lambda _task: waiting_state,
    )

    paused = worker.run_once()

    assert paused is not None
    assert paused.status == "waiting_input"
    assert paused.agent_state["gate_id"] == "input_1"
    resumed_state = waiting_state.model_copy(
        update={"status": "running", "pending_question": None, "gate_id": None}
    )
    resumed = store.resume_task(
        queued.id, state=resumed_state.model_dump(mode="json")
    )
    assert resumed.status == "queued"


def test_insufficient_agent_result_blocks_approval() -> None:
    store = InMemoryEnterpriseStore()
    queued = _queued_task(store)
    queued.result = {"review_result": {"risk_level": "insufficient_evidence", "missing_information": []}}

    assert completion_has_missing_information(queued) is True


def test_grounded_agent_result_can_continue_to_approval() -> None:
    store = InMemoryEnterpriseStore()
    queued = _queued_task(store)
    queued.result = {"review_result": {"risk_level": "medium", "missing_information": []}}

    assert completion_has_missing_information(queued) is False
