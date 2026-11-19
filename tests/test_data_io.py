from pathlib import Path

from law_agent.data.io import read_manifest, write_manifest
from law_agent.data.schemas import SourceRecord
from law_agent.review.io import (
    read_retrieval_traces,
    read_review_cases,
    read_review_results,
    write_retrieval_traces,
    write_review_cases,
    write_review_results,
)
from law_agent.review.schemas import (
    EvidenceSelfCheck,
    MaterialRecord,
    RetrievalTrace,
    ReviewCase,
    ReviewFacts,
    ReviewResult,
)


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


def test_review_artifacts_roundtrip(tmp_path: Path) -> None:
    facts = ReviewFacts()
    case = ReviewCase(
        review_case_id="review_test",
        created_at="2026-07-06T00:00:00+00:00",
        question="这个场景是否需要数据出境安全评估？",
        material=MaterialRecord(material_text="手机号发送给新加坡服务商。"),
        review_facts=facts,
        trace_id="trace_test",
        latest_result_id="result_test",
    )
    trace = RetrievalTrace(
        trace_id="trace_test",
        review_case_id="review_test",
        created_at="2026-07-06T00:00:00+00:00",
        evidence_self_check=EvidenceSelfCheck(status="not_checked"),
    )
    result = ReviewResult(
        review_result_id="result_test",
        review_case_id="review_test",
        trace_id="trace_test",
        risk_level="insufficient_evidence",
        decision_summary="审查任务尚未运行证据检索，目前没有足够依据形成审批结论；请等待事实提取、法源检索和证据自检完成。",
        conclusion="Review case created. Evidence retrieval has not run yet.",
        review_facts=facts,
    )

    case_path = tmp_path / "review_cases.jsonl"
    trace_path = tmp_path / "retrieval_traces.jsonl"
    result_path = tmp_path / "review_results.jsonl"

    assert write_review_cases(case_path, [case]) == 1
    assert write_retrieval_traces(trace_path, [trace]) == 1
    assert write_review_results(result_path, [result]) == 1

    assert read_review_cases(case_path) == [case]
    assert read_retrieval_traces(trace_path) == [trace]
    assert read_review_results(result_path) == [result]
