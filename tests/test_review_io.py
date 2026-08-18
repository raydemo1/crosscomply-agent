from pathlib import Path

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
