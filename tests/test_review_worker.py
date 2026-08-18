"""Tests for the persistent review worker boundary."""

from law_agent.review.enterprise_store import InMemoryEnterpriseStore
from law_agent.review.llm import ReviewWorkflowFailed
from law_agent.review.worker import ReviewWorker


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
    rules = store.create_rule_snapshot(
        case_id="case_001",
        material_snapshot_id=snapshot.id,
        ruleset_version="2026.08",
        facts={},
        determination={"candidate_paths": ["standard_contract_or_certification"]},
    )
    return store.enqueue_review_task(
        case_id="case_001",
        material_snapshot_id=snapshot.id,
        rule_snapshot_id=rules.id,
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
