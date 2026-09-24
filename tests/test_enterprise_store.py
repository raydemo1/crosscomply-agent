import pytest

from law_agent.review.enterprise_store import InMemoryEnterpriseStore


def test_material_versions_are_append_only_and_snapshots_are_immutable() -> None:
    store = InMemoryEnterpriseStore()
    first = store.create_material_version(
        case_id="case_1",
        logical_name="vendor_dpa",
        filename="dpa-v1.pdf",
        content_type="application/pdf",
        object_key="cases/case_1/materials/dpa/v1.pdf",
        sha256="a" * 64,
        byte_size=100,
        uploaded_by="user_1",
    )
    second = store.create_material_version(
        case_id="case_1",
        logical_name="vendor_dpa",
        filename="dpa-v2.pdf",
        content_type="application/pdf",
        object_key="cases/case_1/materials/dpa/v2.pdf",
        sha256="b" * 64,
        byte_size=120,
        uploaded_by="user_1",
    )

    assert first.version_number == 1
    assert second.version_number == 2

    snapshot = store.create_material_snapshot(
        case_id="case_1",
        version_ids=[first.id],
        created_by="user_1",
    )
    duplicate = store.create_material_snapshot(
        case_id="case_1",
        version_ids=[first.id],
        created_by="user_1",
    )
    newer = store.create_material_snapshot(
        case_id="case_1",
        version_ids=[second.id],
        created_by="user_1",
    )

    assert duplicate.id == snapshot.id
    assert snapshot.version_ids == (first.id,)
    assert newer.version_ids == (second.id,)
    assert newer.fingerprint != snapshot.fingerprint


def test_review_task_enqueue_is_idempotent_for_same_frozen_inputs() -> None:
    store = InMemoryEnterpriseStore()
    version = store.create_material_version(
        case_id="case_1",
        logical_name="data_inventory",
        filename="inventory.xlsx",
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        object_key="cases/case_1/materials/inventory/v1.xlsx",
        sha256="c" * 64,
        byte_size=80,
        uploaded_by="user_1",
    )
    snapshot = store.create_material_snapshot(
        case_id="case_1",
        version_ids=[version.id],
        created_by="user_1",
    )
    rule = store.create_rule_snapshot(
        case_id="case_1",
        material_snapshot_id=snapshot.id,
        ruleset_version="cn-cross-border-2026.08",
        facts={"ciio": False},
        determination={"candidate_paths": ["standard_contract_or_certification"]},
    )

    first = store.enqueue_review_task(
        case_id="case_1",
        material_snapshot_id=snapshot.id,
        rule_snapshot_id=rule.id,
        model_id="approved-model-v1",
        data_boundary_summary={"deployment": "intranet"},
    )
    second = store.enqueue_review_task(
        case_id="case_1",
        material_snapshot_id=snapshot.id,
        rule_snapshot_id=rule.id,
        model_id="approved-model-v1",
        data_boundary_summary={"deployment": "intranet"},
    )

    assert second.id == first.id
    assert first.status == "queued"


def test_failed_task_preserves_attempt_and_can_be_retried() -> None:
    store = InMemoryEnterpriseStore()
    version = store.create_material_version(
        case_id="case_1",
        logical_name="contract",
        filename="contract.pdf",
        content_type="application/pdf",
        object_key="contract.pdf",
        sha256="d" * 64,
        byte_size=10,
        uploaded_by="user_1",
    )
    snapshot = store.create_material_snapshot(
        case_id="case_1",
        version_ids=[version.id],
        created_by="user_1",
    )
    rule = store.create_rule_snapshot(
        case_id="case_1",
        material_snapshot_id=snapshot.id,
        ruleset_version="v1",
        facts={},
        determination={},
    )
    task = store.enqueue_review_task(
        case_id="case_1",
        material_snapshot_id=snapshot.id,
        rule_snapshot_id=rule.id,
        model_id="model-v1",
        data_boundary_summary={},
    )

    claimed = store.claim_next_task(worker_id="worker-1")
    assert claimed is not None
    assert claimed.id == task.id
    assert claimed.status == "running"
    assert claimed.attempt_count == 1

    failed = store.fail_task(
        task.id,
        failed_node="evidence_retrieval",
        error_category="dependency_unavailable",
        error_message="Elasticsearch unavailable",
    )
    assert failed.status == "failed"
    assert failed.current_node == "evidence_retrieval"
    assert failed.attempts[-1].error_category == "dependency_unavailable"

    store.retry_task(task.id)
    retried = store.claim_next_task(worker_id="worker-2")
    assert retried is not None
    assert retried.attempt_count == 2
    assert len(retried.attempts) == 2


def test_expired_running_task_is_requeued_with_failed_attempt() -> None:
    store = InMemoryEnterpriseStore()
    version = store.create_material_version(
        case_id="case_lease",
        logical_name="contract",
        filename="contract.pdf",
        content_type="application/pdf",
        object_key="contract.pdf",
        sha256="e" * 64,
        byte_size=10,
        uploaded_by="user_1",
    )
    snapshot = store.create_material_snapshot(
        case_id="case_lease", version_ids=[version.id], created_by="user_1"
    )
    rule = store.create_rule_snapshot(
        case_id="case_lease",
        material_snapshot_id=snapshot.id,
        ruleset_version="v1",
        facts={},
        determination={},
    )
    task = store.enqueue_review_task(
        case_id="case_lease",
        material_snapshot_id=snapshot.id,
        rule_snapshot_id=rule.id,
        model_id="model-v1",
        data_boundary_summary={},
    )
    claimed = store.claim_next_task(worker_id="dead-worker", lease_seconds=-1)
    assert claimed is not None

    assert store.requeue_expired_tasks() == 1
    recovered = store.get_task(task.id)
    assert recovered is not None
    assert recovered.status == "queued"
    assert recovered.error_category == "worker_lease_expired"
    assert recovered.attempts[-1].status == "failed"


def test_snapshot_rejects_two_versions_of_the_same_material() -> None:
    store = InMemoryEnterpriseStore()
    first = store.create_material_version(
        case_id="case_1",
        logical_name="vendor_dpa",
        filename="dpa-v1.pdf",
        content_type="application/pdf",
        object_key="cases/case_1/materials/dpa/v1.pdf",
        sha256="a" * 64,
        byte_size=100,
        uploaded_by="user_1",
    )
    second = store.create_material_version(
        case_id="case_1",
        logical_name="vendor_dpa",
        filename="dpa-v2.pdf",
        content_type="application/pdf",
        object_key="cases/case_1/materials/dpa/v2.pdf",
        sha256="b" * 64,
        byte_size=120,
        uploaded_by="user_1",
    )

    # A frozen snapshot must describe one state of the case, so it carries at most one version
    # of each logical material; picking the newest version is the caller's job.
    with pytest.raises(ValueError, match="同一份逻辑材料"):
        store.create_material_snapshot(
            case_id="case_1",
            version_ids=[first.id, second.id],
            created_by="user_1",
        )

    # Distinct materials are still allowed side by side in one frozen snapshot.
    other = store.create_material_version(
        case_id="case_1",
        logical_name="data_inventory",
        filename="inventory.md",
        content_type="text/markdown",
        object_key="cases/case_1/materials/inventory/v1.md",
        sha256="c" * 64,
        byte_size=40,
        uploaded_by="user_1",
    )
    multi = store.create_material_snapshot(
        case_id="case_1",
        version_ids=[first.id, other.id],
        created_by="user_1",
    )
    assert set(multi.version_ids) == {first.id, other.id}


def test_supersede_waiting_tasks_closes_paused_runs() -> None:
    store = InMemoryEnterpriseStore()
    version = store.create_material_version(
        case_id="case_1",
        logical_name="contract",
        filename="contract.pdf",
        content_type="application/pdf",
        object_key="contract.pdf",
        sha256="f" * 64,
        byte_size=10,
        uploaded_by="user_1",
    )
    snapshot = store.create_material_snapshot(
        case_id="case_1", version_ids=[version.id], created_by="user_1"
    )
    rule = store.create_rule_snapshot(
        case_id="case_1",
        material_snapshot_id=snapshot.id,
        ruleset_version="v1",
        facts={},
        determination={},
    )
    task = store.enqueue_review_task(
        case_id="case_1",
        material_snapshot_id=snapshot.id,
        rule_snapshot_id=rule.id,
        model_id="model-v1",
        data_boundary_summary={},
    )
    store.claim_next_task(worker_id="worker-1")
    paused = store.pause_task(task.id, state={"steps": [], "question": "请确认累计出境人数。"})
    assert paused.status == "waiting_input"

    assert store.supersede_waiting_tasks("case_1") == [task.id]
    superseded = store.get_task(task.id)
    assert superseded is not None
    assert superseded.status == "superseded"
    assert superseded.attempts[-1].status == "superseded"

    # Already-closed runs are left alone, and a superseded task no longer blocks new work.
    assert store.supersede_waiting_tasks("case_1") == []
    assert (
        store.enqueue_review_task(
            case_id="case_1",
            material_snapshot_id=snapshot.id,
            rule_snapshot_id=rule.id,
            model_id="model-v2",
            data_boundary_summary={},
        ).status
        == "queued"
    )
