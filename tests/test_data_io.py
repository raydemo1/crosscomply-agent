from pathlib import Path

from law_agent.data.io import read_manifest, write_manifest
from law_agent.data.schemas import SourceRecord


def test_manifest_round_trip_preserves_all_list_metadata(tmp_path: Path) -> None:
    record = SourceRecord(
        source_id="subject-metadata-source",
        title="适用对象元数据回归测试",
        source_url="https://example.test/source",
        source_site="example.test",
        doc_type="guideline",
        legal_domain=["数据合规", "数据出境"],
        applicable_subjects=["数据处理者", "跨境电商平台经营者"],
        contract_parties=["委托方", "受托方"],
        topic_tags=["数据出境", "负面清单"],
    )
    path = tmp_path / "source_manifest.csv"

    write_manifest(path, [record])
    restored = read_manifest(path)

    assert restored[0].legal_domain == record.legal_domain
    assert restored[0].applicable_subjects == record.applicable_subjects
    assert restored[0].contract_parties == record.contract_parties
    assert restored[0].topic_tags == record.topic_tags
    assert path.read_text(encoding="utf-8").count("[") == 0
