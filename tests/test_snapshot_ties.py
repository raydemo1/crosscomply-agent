"""The latest in-memory snapshot follows creation order when timestamps tie."""

from dataclasses import replace

import pytest

from law_agent.review.enterprise_store import (
    InMemoryEnterpriseStore,
    IntakeSnapshot,
    MaterialSnapshot,
)


@pytest.mark.parametrize("kind", ["material", "intake"])
def test_equal_timestamp_uses_creation_order_not_random_id(kind):
    store = InMemoryEnterpriseStore()
    common = {"case_id": "case", "created_by": "user", "created_at": "2026-10-22T10:00:00+00:00"}
    if kind == "material":
        old = MaterialSnapshot(id="snapshot_z", fingerprint="old", version_ids=("v1",), **common)
        new = replace(old, id="snapshot_a", fingerprint="new", version_ids=("v2",))
        collection = store.snapshots
        latest = lambda: store.get_latest_material_snapshot("case")
    else:
        old = IntakeSnapshot(id="snapshot_z", material_snapshot_id="material", fingerprint="old", intake={}, **common)
        new = replace(old, id="snapshot_a", fingerprint="new", intake={"business_activity": "new"})
        collection = store.intake_snapshots
        latest = lambda: store.get_latest_intake_snapshot(case_id="case", material_snapshot_id="material")
    collection[old.id] = old
    collection[new.id] = new
    assert latest() == new
    earlier = replace(old, id="earlier", created_at="2026-10-21T10:00:00+00:00")
    collection[earlier.id] = earlier
    assert latest() == new
