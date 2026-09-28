"""Golden-set references must resolve against the published corpus manifest.

The manifest is the single source of truth for a source's ``citation_role``.
These tests fail loudly when a golden case still points at a retired source
identity, or when a regional/industry case expects a role the manifest no
longer assigns — the regression that silently disabled the region/industry
retrieval boost.
"""

from __future__ import annotations

import pytest

from law_agent.data.citation_policy import CLAUSE_LEVEL_DOC_TYPES, can_cite_clause
from law_agent.data.io import read_manifest
from law_agent.data.schemas import SourceRecord
from law_agent.review.evalset.cases import get_default_scenarios
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH

MANIFEST_PATH = DEFAULT_CHUNKS_PATH.parent / "source_manifest.csv"
CONDITIONAL_ROLES = {"conditional_local_basis", "conditional_industry_basis"}


def _manifest_records() -> list[SourceRecord]:
    if not MANIFEST_PATH.exists():
        pytest.skip(f"语料未发布：{MANIFEST_PATH}")
    return read_manifest(MANIFEST_PATH)


def _manifest_roles() -> dict[str, str]:
    return {record.source_id: record.citation_role for record in _manifest_records()}


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


def test_corpus_citation_permission_follows_the_instrument_not_the_role() -> None:
    """引用权限由材料本身决定，不由 citation_role 单独决定。

    已验证的法规/规章/司法解释不因 ``conditional_*``（仅表示适用范围受限）
    而失去条款引用能力；指南、标准、政策问答、负面清单也不会被批量赋予法规效力。
    """

    records = _manifest_records()

    for record in records:
        expected = record.library_kind == "legal" and (
            record.citation_role == "primary_legal_basis"
            or (
                record.citation_role in CONDITIONAL_ROLES
                and record.doc_type in CLAUSE_LEVEL_DOC_TYPES
            )
        )
        assert can_cite_clause(record) is expected, record.source_id

    # 地方性法规：条款有效力，只是适用范围限于深圳。
    shenzhen = next(r for r in records if r.source_id == "shenzhen_data_regulation_2021")
    assert shenzhen.doc_type == "regulation"
    assert shenzhen.citation_role == "conditional_local_basis"
    assert can_cite_clause(shenzhen) is True

    # 负面清单：管理性清单，可引用其内容但不作为条款级法律依据。
    lists = [
        record
        for record in records
        if record.citation_role == "conditional_local_basis" and record.doc_type == "guideline"
    ]
    assert lists
    assert all(can_cite_clause(record) is False for record in lists)
