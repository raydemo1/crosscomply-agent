"""Golden-set references must resolve against the published corpus manifest.

The manifest is the single source of truth for a source's ``citation_role``.
These tests fail loudly when a golden case still points at a retired source
identity, or when a regional/industry case expects a role the manifest no
longer assigns — the regression that silently disabled the region/industry
retrieval boost.
"""

from __future__ import annotations

import pytest

from law_agent.data.io import read_manifest
from law_agent.review.evalset.cases import get_default_scenarios
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH

MANIFEST_PATH = DEFAULT_CHUNKS_PATH.parent / "source_manifest.csv"
CONDITIONAL_ROLES = {"conditional_local_basis", "conditional_industry_basis"}


def _manifest_roles() -> dict[str, str]:
    if not MANIFEST_PATH.exists():
        pytest.skip(f"语料未发布：{MANIFEST_PATH}")
    return {record.source_id: record.citation_role for record in read_manifest(MANIFEST_PATH)}


def test_golden_cases_reference_published_sources() -> None:
    roles = _manifest_roles()
    missing = sorted(
        {
            source_id
            for scenario in get_default_scenarios()
            for source_id in [
                *scenario.expected_sources,
                *scenario.must_have_sources,
                *scenario.optional_supporting_sources,
            ]
            if source_id not in roles
        }
    )

    assert missing == []


def test_regional_and_industry_cases_expect_the_manifest_roles() -> None:
    roles = _manifest_roles()
    offenders: list[str] = []
    for scenario in get_default_scenarios():
        if not ({"regional", "industry"} & set(scenario.tags)):
            continue
        declared = set(scenario.expected_citation_roles)
        sources = [*scenario.expected_sources, *scenario.must_have_sources]
        actual = {roles[source_id] for source_id in sources}
        for source_id in [*scenario.must_have_sources, *scenario.optional_supporting_sources]:
            role = roles[source_id]
            if role in CONDITIONAL_ROLES and role not in declared:
                offenders.append(f"{scenario.case_id}: {source_id} -> 未声明的 {role}")
        for role in declared & CONDITIONAL_ROLES:
            if role not in actual:
                offenders.append(f"{scenario.case_id}: 声明了 {role}，但预期来源均非该角色")

    assert offenders == []
