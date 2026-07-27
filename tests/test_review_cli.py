from pathlib import Path

import pytest

from law_agent.review.cli import main
from law_agent.review.io import read_review_cases, read_review_results, read_retrieval_traces
from law_agent.review.schemas import ReviewFacts, RetrievalQuery
from law_agent.review.service import create_review_case


@pytest.fixture(autouse=True)
def stub_llm_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_facts(material_text: str, question: str | None = None) -> ReviewFacts:
        return ReviewFacts(
            data_types=[
                data_type
                for term, data_type in (("手机号", "手机号"), ("定位", "定位信息"))
                if term in material_text
            ],
            cross_border_transfer=(
                True if any(term in material_text for term in ("出境", "新加坡", "境外")) else None
            ),
            overseas_recipient="新加坡" if "新加坡" in material_text else None,
            processing_purpose="推荐优化" if "推荐优化" in material_text else None,
            industry="智能网联汽车" if "汽车" in material_text else None,
            region="上海" if "上海" in material_text else None,
            missing_information=["legal_basis_or_consent"],
        )

    def fake_queries(question: str, facts: ReviewFacts, material_text: str | None = None):
        queries = [
            RetrievalQuery(
                query_id="q_test",
                query_type="legal_issue",
                text=question,
            ),
            RetrievalQuery(
                query_id="q_material",
                query_type="material_fact",
                text=" ".join(facts.data_types) or question,
            ),
        ]
        if facts.industry:
            queries.append(RetrievalQuery(query_id="q_industry", query_type="industry_condition", text=facts.industry))
        if facts.region:
            queries.append(RetrievalQuery(query_id="q_region", query_type="region_condition", text=facts.region))
        if len(queries) < 3:
            queries.append(RetrievalQuery(query_id="q_missing", query_type="missing_information", text="补充事实"))
        return queries

    monkeypatch.setattr("law_agent.review.service.extract_facts_with_deepseek", fake_facts)
    monkeypatch.setattr("law_agent.review.service.plan_queries_with_deepseek", fake_queries)


def test_create_review_case_writes_case_trace_and_result(tmp_path: Path) -> None:
    response = create_review_case(
        question="这个场景是否需要数据出境安全评估？",
        material_text="我们会将手机号和定位信息发送给新加坡服务商用于推荐优化。",
        output_dir=tmp_path,
        now=lambda: "2026-07-06T00:00:00+00:00",
        id_factory=lambda prefix: f"{prefix}_test",
    )

    assert response.review_case.review_case_id == "review_test"
    assert response.trace.trace_id == "trace_test"
    assert response.result.review_result_id == "result_test"
    assert response.review_case.latest_result_id == "result_test"
    assert response.result.risk_level == "insufficient_evidence"
    assert response.trace.evidence_self_check.status == "not_checked"

    assert read_review_cases(response.case_path) == [response.review_case]
    assert read_retrieval_traces(response.trace_path) == [response.trace]
    assert read_review_results(response.result_path) == [response.result]


def test_review_cli_run_material_text(tmp_path: Path, capsys) -> None:
    exit_code = main(
        [
            "run",
            "--question",
            "这个场景是否需要数据出境安全评估？",
            "--material-text",
            "手机号发送给新加坡服务商。",
            "--output-dir",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Created review case review_" in captured.out
    assert "Trace trace_" in captured.out
    assert "Result result_" in captured.out
    assert (tmp_path / "review_cases.jsonl").exists()
    assert (tmp_path / "retrieval_traces.jsonl").exists()
    assert (tmp_path / "review_results.jsonl").exists()


def test_review_cli_rejects_blank_question(tmp_path: Path, capsys) -> None:
    exit_code = main(
        [
            "run",
            "--question",
            " ",
            "--material-text",
            "手机号发送给新加坡服务商。",
            "--output-dir",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "question must not be blank" in captured.err


def test_review_cli_requires_material_text(tmp_path: Path, capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "run",
                "--question",
                "这个场景是否需要数据出境安全评估？",
                "--output-dir",
                str(tmp_path),
            ]
        )

    captured = capsys.readouterr()
    assert exc_info.value.code == 2
    assert "one of the arguments --material-text --material-file is required" in captured.err


def test_review_cli_run_material_file(tmp_path: Path, capsys) -> None:
    material_path = tmp_path / "scenario.txt"
    material_path.write_text("手机号发送给新加坡服务商。", encoding="utf-8")

    exit_code = main(
        [
            "run",
            "--question",
            "这个场景是否需要数据出境安全评估？",
            "--material-file",
            str(material_path),
            "--output-dir",
            str(tmp_path / "runs"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Created review case review_" in captured.out
    assert (tmp_path / "runs" / "review_cases.jsonl").exists()


def test_create_review_case_populates_facts_from_material(tmp_path: Path) -> None:
    response = create_review_case(
        question="这个场景是否需要数据出境安全评估？",
        material_text="我们会将手机号和定位信息发送给新加坡服务商用于推荐优化。",
        output_dir=tmp_path,
        now=lambda: "2026-07-06T00:00:00+00:00",
        id_factory=lambda prefix: f"{prefix}_test",
    )

    facts = response.review_case.review_facts
    assert facts.cross_border_transfer is True
    assert facts.overseas_recipient == "新加坡"
    assert "手机号" in facts.data_types
    assert "定位信息" in facts.data_types
    assert facts.processing_purpose is not None
    assert "legal_basis_or_consent" in facts.missing_information

    assert response.result.review_facts == facts


def test_create_review_case_persists_queries_in_trace(tmp_path: Path) -> None:
    response = create_review_case(
        question="这个场景是否需要数据出境安全评估？",
        material_text="我们会将手机号和定位信息发送给新加坡服务商用于推荐优化。",
        output_dir=tmp_path,
        now=lambda: "2026-07-06T00:00:00+00:00",
        id_factory=lambda prefix: f"{prefix}_test",
    )

    queries = response.trace.queries
    assert len(queries) >= 3
    query_types = [q.query_type for q in queries]
    assert "legal_issue" in query_types
    assert "material_fact" in query_types
    assert "missing_information" in query_types

    assert all(q.query_id.startswith("q_") for q in queries)


def test_create_review_case_with_automotive_material_extracts_industry(tmp_path: Path) -> None:
    response = create_review_case(
        question="汽车数据出境合规要求？",
        material_text="智能网联汽车采集车辆位置和行驶轨迹数据，需进行数据出境安全评估。",
        output_dir=tmp_path,
    )

    assert response.review_case.review_facts.industry == "智能网联汽车"
    query_types = [q.query_type for q in response.trace.queries]
    assert "industry_condition" in query_types


def test_create_review_case_with_regional_material_extracts_region(tmp_path: Path) -> None:
    response = create_review_case(
        question="上海数据出境负面清单要求？",
        material_text="公司在上海自贸区开展业务，涉及数据出境。",
        output_dir=tmp_path,
    )

    assert response.review_case.review_facts.region == "上海"
    query_types = [q.query_type for q in response.trace.queries]
    assert "region_condition" in query_types


def test_create_review_case_supports_custom_facts_extractor(tmp_path: Path) -> None:
    custom_facts = ReviewFacts(
        business_activity="custom activity",
        data_types=["自定义数据"],
        cross_border_transfer=True,
    )

    def custom_extractor(material_text: str, question: str | None = None) -> ReviewFacts:
        return custom_facts

    response = create_review_case(
        question="问题",
        material_text="材料",
        output_dir=tmp_path,
        facts_extractor=custom_extractor,
    )

    assert response.review_case.review_facts == custom_facts
    assert response.result.review_facts == custom_facts


def test_create_review_case_llm_mode_uses_llm_nodes(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []
    llm_facts = ReviewFacts(
        business_activity="llm activity",
        data_types=["手机号"],
        cross_border_transfer=True,
        overseas_recipient="新加坡",
        missing_information=[],
    )

    def fake_extract(material_text: str, question: str | None = None) -> ReviewFacts:
        calls.append("facts")
        return llm_facts

    def fake_plan(question: str, facts: ReviewFacts, material_text: str | None = None):
        calls.append("queries")
        from law_agent.review.schemas import RetrievalQuery

        return [
            RetrievalQuery(
                query_id="q_1",
                query_type="legal_issue",
                text="数据出境安全评估",
            )
        ]

    monkeypatch.setattr("law_agent.review.service.extract_facts_with_deepseek", fake_extract)
    monkeypatch.setattr("law_agent.review.service.plan_queries_with_deepseek", fake_plan)

    response = create_review_case(
        question="问题",
        material_text="材料",
        output_dir=tmp_path,
        review_mode="llm",
    )

    assert calls == ["facts", "queries"]
    assert response.review_case.review_facts == llm_facts
    assert response.trace.queries[0].text == "数据出境安全评估"


def test_review_cli_index_service_writes_index_artifacts(
    tmp_path: Path,
    capsys,
) -> None:
    from law_agent.data.io import write_jsonl
    from tests.test_review_retrieval_keyword import FIXTURE_CHUNKS

    chunks_path = tmp_path / "chunks.jsonl"
    es_path = tmp_path / "es_bulk.ndjson"
    pg_path = tmp_path / "pg_rows.jsonl"
    write_jsonl(chunks_path, FIXTURE_CHUNKS[:1])

    exit_code = main(
        [
            "index-service",
            "--chunks",
            str(chunks_path),
            "--elasticsearch-output",
            str(es_path),
            "--pgvector-output",
            str(pg_path),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Elasticsearch bulk" in captured.out
    assert es_path.exists()
    assert pg_path.exists()
    assert FIXTURE_CHUNKS[0].chunk_id in es_path.read_text(encoding="utf-8")
    assert FIXTURE_CHUNKS[0].chunk_id in pg_path.read_text(encoding="utf-8")
