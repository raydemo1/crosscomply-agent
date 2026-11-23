import csv
from datetime import date

import pytest

from law_agent.data.schemas import SourceRecord
from law_agent.data.source_rules import metadata_problems, require_consistent_metadata
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH


def _source(**fields) -> SourceRecord:
    base = dict(source_id="s", title="示例规定", source_url="https://www.cac.gov.cn/x", source_site="cac.gov.cn",
                doc_type="regulation", authority="departmental_rule", citation_role="primary_legal_basis",
                law_status="effective", publish_date="2024-03-22", effective_date="2024-03-22",
                issuing_body="国家互联网信息办公室")
    return SourceRecord(**(base | fields))


def test_a_consistent_departmental_rule_passes() -> None:
    assert metadata_problems(_source(), today=date(2026, 1, 1)) == []


@pytest.mark.parametrize(("fields", "problem"), [
    ({"doc_type": "faq", "authority": "public_interpretation"}, "主要法规依据"),
    ({"doc_type": "guideline", "authority": "departmental_rule", "citation_role": "implementation_reference"}, "效力层级"),
    ({"citation_role": "conditional_local_basis"}, "适用地区"),
    ({"doc_type": "policy", "authority": "local_normative_document", "applicable_region": "CN-BJ",
      "citation_role": "implementation_reference"}, "地方性条件依据"),
    ({"effective_date": None}, "生效日期"),
    ({"issuing_body": "中国（天津）自由贸易试验区"}, "不是机关名称"),
    ({"title": "示例规定_中国政府网"}, "网页后缀"),
    ({"effective_date": "2027-01-01"}, "not_yet_effective"),
])
def test_each_contradiction_is_named(fields, problem) -> None:
    problems = metadata_problems(_source(**fields), today=date(2026, 1, 1))
    assert any(problem in item for item in problems), problems
    with pytest.raises(ValueError):
        require_consistent_metadata(_source(**fields))


def test_the_published_corpus_metadata_is_consistent() -> None:
    manifest = DEFAULT_CHUNKS_PATH.parent / "source_manifest.csv"
    if not manifest.exists():
        pytest.skip("corpus not present")
    problems = {}
    for row in csv.DictReader(manifest.open(encoding="utf-8")):
        source = SourceRecord.model_validate({key: value for key, value in row.items() if key in SourceRecord.model_fields})
        if found := metadata_problems(source):
            problems[source.source_id] = found
    assert problems == {}
