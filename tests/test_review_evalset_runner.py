"""Tests for the service-only evaluation runner."""

from pathlib import Path

import law_agent.review.evalset.runner as runner_module
from law_agent.review.evalset.cases import get_default_scenarios
from law_agent.review.evalset.runner import (
    EvalCaseInput,
    _read_eval_inputs,
    _run_single_case_safely,
    format_summary_markdown,
)
from law_agent.review.evalset.schemas import EvalSummary, ModeMetrics
from law_agent.review.llm import ReviewWorkflowFailed
from law_agent.review.schemas import RetrievalQuery, ReviewFacts


def test_eval_inputs_round_trip_for_fair_workflow_comparison(tmp_path: Path) -> None:
    path = tmp_path / "full_llm_inputs.jsonl"
    path.write_text(
        '{"case_id":"case_1","facts":{"business_activity":"测试",'
        '"data_types":[],"sensitive_personal_info":null,'
        '"cross_border_transfer":true,"overseas_recipient":null,'
        '"processing_purpose":null,"legal_basis_or_consent":null,'
        '"industry":null,"region":null,"missing_information":[]},'
        '"queries":[{"query_id":"q_1","query_type":"legal_issue",'
        '"text":"数据出境"}]}\n',
        encoding="utf-8",
    )

    loaded = _read_eval_inputs(path)
    assert loaded["case_1"] == EvalCaseInput(
        facts=ReviewFacts(business_activity="测试", cross_border_transfer=True),
        queries=[
            RetrievalQuery(
                query_id="q_1",
                query_type="legal_issue",
                text="数据出境",
            )
        ],
    )


def test_markdown_report_contains_core_metrics_and_bad_cases() -> None:
    summary = EvalSummary(
        generated_at="2026-07-11T00:00:00+00:00",
        chunks_path="data/corpus/chunks.jsonl",
        cases_path="full",
        mode_metrics={
            "retrieval=service,review=llm": ModeMetrics(
                mode="retrieval=service,review=llm",
                mean_recall_at_3=0.75,
                mean_recall_at_5=0.85,
                mean_mrr_at_10=0.9,
                abstention_accuracy=1.0,
                bad_case_count=0,
                total_cases=82,
            )
        },
    )

    report = format_summary_markdown(summary)
    assert "# LawAgent Full Evaluation Report" in report
    assert "| Recall@5 | 0.8500 |" in report
    assert "No bad cases" in report


def test_eval_records_workflow_failure_without_aborting_suite(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scenario = get_default_scenarios()[0]

    def fail_case(*args, **kwargs):
        raise ReviewWorkflowFailed(
            failed_node="result_generation",
            reason="claim_grounding_validation_failed",
            message="unsupported claim",
            attempts=1,
            trace_id="trace_failed",
        )

    monkeypatch.setattr(runner_module, "_run_single_case", fail_case)
    result = _run_single_case_safely(
        scenario,
        tmp_path / "chunks.jsonl",
        review_mode="llm",
        top_k=10,
    )

    assert result.is_bad_case is True
    assert result.workflow_failed is True
    assert result.failed_node == "result_generation"
    assert result.failure_reason == "claim_grounding_validation_failed"
