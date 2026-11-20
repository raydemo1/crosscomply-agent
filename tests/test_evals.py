import json
import shutil
from datetime import date
from pathlib import Path

import pytest
from pydantic import ValidationError

from law_agent.data.schemas import SourceRecord
from law_agent.evals.agent_eval import grade_delivery, run_agent_eval
from law_agent.evals.arms import ArmContext, CrossComplyArm, DirectArm, ImportedArm, WebAgentArm
from law_agent.evals.bank.checks import CorpusIndex, check_bank, completeness_errors
from law_agent.evals.bank.loader import load_personas, load_scenarios, materialize
from law_agent.evals.bank.schema import (
    AgentRubric,
    MaterialFile,
    RetrievalLabels,
    Scenario,
    ScriptedAnswer,
    scenario_hash,
)
from law_agent.evals.delivery import CitedSource, Delivery, FreeTextAnswer
from law_agent.evals.grading import JudgeVerdict, classify, deterministic_grade, resolve_citations
from law_agent.evals.report import agent_report, retrieval_report
from law_agent.evals.retrieval import aggregate, score_scenario
from law_agent.evals.review import (
    export_review_package,
    lock_test_split,
    record_review,
    review_errors,
)
from law_agent.evals.runs import RunStore, wilson_interval
from law_agent.review.agent import AgentState, AgentStep
from law_agent.review.materials import material_from_file
from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH, load_corpus
from law_agent.review.schemas import RetrievalHit

FLOW = "cac_cross_border_data_flow_rules_2024"
PIPL = "flk_npc_ff8081817b6472a3017b656cc2040044"
QNA = "cac_data_export_policy_qna_2025_10"
SOURCES = [
    SourceRecord(source_id=FLOW, title="促进和规范数据跨境流动规定", source_url="https://x/1", source_site="x",
                 doc_type="regulation", citation_role="primary_legal_basis", authority="administrative_regulation"),
    SourceRecord(source_id=PIPL, title="中华人民共和国个人信息保护法", source_url="https://x/2", source_site="x",
                 doc_type="law", citation_role="primary_legal_basis", authority="national_law"),
    SourceRecord(source_id=QNA, title="数据出境安全管理政策问答（2025年10月）", source_url="https://x/3",
                 source_site="x", doc_type="faq", citation_role="interpretation_auxiliary"),
]


def scenario(**overrides) -> Scenario:
    values = {
        "scenario_id": "t_001", "split": "dev", "persona_id": "ota_shanghai", "as_of_date": date(2026, 10, 15),
        "question": "是否需要申报？", "judgment_points": ["mechanism.threshold"],
        "materials": [MaterialFile(filename="说明.txt", kind="email", text="12万人")],
        "fact_pattern_source": {"kind": "synthetic", "public_facts": "常见错误", "synthesized": "全部虚构"},
        "selection_reason": "门槛", "reference_basis": ["2024规定第八条"],
        "agent_rubric": AgentRubric(should_abstain=None, must_cover_sources=[FLOW], forbidden_clause_sources=[QNA],
                                    allowed_judgments=["标准合同"], forbidden_judgments=["安全评估"]),
    }
    values.update(overrides)
    return Scenario.model_validate(values)


# --------------------------------------------------------------------------- bank


def test_bank_loads_and_samples_are_complete() -> None:
    personas = load_personas()
    scenarios = load_scenarios(field_dir=None)
    assert scenarios and all(s.persona_id is None or s.persona_id in personas for s in scenarios)
    assert all(not completeness_errors(s) for s in scenarios)
    assert check_bank(scenarios, None, set(personas)) == []


@pytest.mark.skipif(not DEFAULT_CHUNKS_PATH.exists(), reason="corpus is not in Git")
def test_bank_labels_are_consistent_with_corpus() -> None:
    scenarios = load_scenarios(field_dir=None)
    assert check_bank(scenarios, CorpusIndex(load_corpus()), set(load_personas())) == []


def test_materialized_bundle_parses_like_an_upload(tmp_path) -> None:
    sample = next(s for s in load_scenarios(field_dir=None) if any(m.sheets for m in s.materials))
    for path in materialize(sample, tmp_path):
        text = material_from_file(path).material_text
        assert text.strip() and (path.suffix != ".xlsx" or "## 工作表" in text)


def test_schema_rejects_unlocated_citations_mixed_bodies_and_unlabelled_scenarios() -> None:
    with pytest.raises(ValidationError):
        RetrievalLabels(queries=["q"], must_cite=[{"source_id": "s", "note": "n"}])
    with pytest.raises(ValidationError):
        MaterialFile(filename="a.xlsx", kind="data_inventory", text="x")
    with pytest.raises(ValidationError):
        scenario(agent_rubric=None)
    assert ("rubric.allowed_judgments", "请填写此项") in completeness_errors(
        scenario(agent_rubric=AgentRubric(forbidden_judgments=["x"])))


# --------------------------------------------------------------------------- review


def test_review_record_lock_and_tamper_detection(tmp_path) -> None:
    bank = tmp_path / "bank"
    (bank / "scenarios").mkdir(parents=True)
    test_case = scenario(scenario_id="t_test", split="test")
    (bank / "scenarios" / "t_test.json").write_text(test_case.model_dump_json(), encoding="utf-8")
    reviews, lock = bank / "reviews.json", bank / "test_lock.json"

    package = tmp_path / "package"
    export_review_package([test_case], package)
    filled = json.loads((package / "review.json").read_text(encoding="utf-8"))
    filled[0].update(decision="approved", reviewer="法务甲", reviewed_at="2026-11-20")
    (package / "review.json").write_text(json.dumps(filled, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="尚未审定"):
        lock_test_split([test_case], lock_path=lock)
    assert record_review(package / "review.json", [test_case], bank_dir=bank, reviews_path=reviews) == 1
    approved = Scenario.model_validate_json((bank / "scenarios" / "t_test.json").read_text(encoding="utf-8"))
    assert (approved.label_status, approved.reviewer) == ("approved", "法务甲")
    assert scenario_hash(approved) == scenario_hash(test_case)
    assert lock_test_split([approved], lock_path=lock) == 1
    assert review_errors([approved], reviews_path=reviews, lock_path=lock) == []

    edited = approved.model_copy(update={"question": "改过的问题"})
    errors = review_errors([edited], reviews_path=reviews, lock_path=lock)
    assert any("审定后被修改" in e for e in errors) and any("锁定" in e for e in errors)


# --------------------------------------------------------------------------- grading


def test_free_text_citations_resolve_and_feed_deterministic_checks() -> None:
    delivery = Delivery(arm="direct", scenario_id="t_001", status="succeeded", judgment_form="definite",
                        citations=[CitedSource(title="《促进和规范数据跨境流动规定》", article_no="第八条"),
                                   CitedSource(title="数据出境安全管理政策问答（2025年10月）", article_no="第2问"),
                                   CitedSource(title="某不存在的法规", article_no="第一条")])
    resolved = resolve_citations(delivery.citations, SOURCES)
    assert [c.source_id for c in resolved] == [FLOW, QNA, None]
    grade = deterministic_grade(scenario(), delivery.model_copy(update={"citations": resolved}),
                                {s.source_id: s for s in SOURCES})
    assert grade.missing_required_sources == [] and grade.unresolved_citations == 1
    # A Q&A is not a clause even with a locator, so it is not an illegal clause citation either.
    assert grade.illegal_clause_citations == [] and grade.passed


def test_outcome_taxonomy_separates_dangerous_errors() -> None:
    s = scenario()
    ok = Delivery(arm="a", scenario_id="t_001", status="succeeded", judgment_form="conditional")
    det = deterministic_grade(s, ok, {})
    assert det.fail_reasons == ["missing_required_sources"]
    wrong = classify(s, ok.model_copy(update={"judgment_form": "definite"}), det,
                     JudgeVerdict(judgment_form="definite", legal_correctness="fail"))
    assert wrong.legal == "wrong_definite"
    passed = deterministic_grade(s, ok.model_copy(update={"citations": [
        CitedSource(title="t", article_no="第八条", source_id=FLOW, can_cite_clause=True)]}), {})
    assert classify(s, ok, passed, JudgeVerdict(judgment_form="conditional", legal_correctness="pass",
                                                overall_pass=True)).legal == "correct_conditional"
    assert classify(s, ok, passed, JudgeVerdict(error="HTTP 500")).legal == "unevaluated"
    blocked = classify(s, ok.model_copy(update={"status": "blocked"}), None, None)
    assert (blocked.execution, blocked.legal) == ("blocked", "unevaluated")


# --------------------------------------------------------------------------- arms


def _payload(source=FLOW) -> dict:
    return {"review_result": {"risk_level": "medium", "legal_path": "标准合同", "decision_summary": "摘要",
                              "conclusion": "结论", "missing_information": [],
                              "citations": [{"source_id": source, "title": "t", "article_no": "第八条",
                                             "can_cite_clause": True}]},
            "freshness_hold": False}


def _completed() -> AgentState:
    return AgentState(goal="g", status="completed", turns=3, searches=2, reads=1,
                      steps=[AgentStep(number=1, action="search_evidence", summary="s")], result=_payload())


def _waiting() -> AgentState:
    return AgentState(goal="g", status="waiting_input", turns=2, gate_id="input_1", pending_question="口径？",
                      steps=[AgentStep(number=1, action="request_input", summary="口径？")])


def _arm(executor) -> CrossComplyArm:
    return CrossComplyArm(model_id="m", executor_factory=lambda *a, **k: executor)


def test_crosscomply_arm_pauses_answers_resumes_and_completes(tmp_path) -> None:
    events = []
    ctx = ArmContext(chunks_path=tmp_path / "chunks.jsonl", event=lambda kind, value: events.append(kind))
    case = scenario(scripted_answers=[ScriptedAnswer(answer="按自然人去重")],
                    portfolio=None, materials=load_scenarios(field_dir=None)[0].materials)
    states = iter([_waiting(), _completed()])
    delivery = _arm(lambda task, store: next(states)).run(case, ctx)
    assert delivery.status == "succeeded" and delivery.answers_used == ["按自然人去重"]
    assert delivery.questions_asked == ["口径？"] and delivery.citations[0].can_cite_clause is True
    assert delivery.judgment_form == "definite" and delivery.counters["turns"] == 3

    unanswered = _arm(lambda task, store: _waiting()).run(scenario(), ctx)
    assert unanswered.status == "unanswered_gate"

    def broke(task, store):
        raise RuntimeError("LLM request failed with HTTP 402: Insufficient Balance")

    assert _arm(broke).run(scenario(), ctx).status == "blocked"


class _Node:
    def __init__(self, outputs):
        self.outputs = iter(outputs)

    def run(self, messages):
        return next(self.outputs)


class _Search:
    def search(self, query, *, max_results):
        from law_agent.review.web_research import WebSearchResult

        return [WebSearchResult(url="https://example.test/a", title="a", text="正文")]


def test_baseline_arms_normalize_answers(tmp_path) -> None:
    from law_agent.evals.arms.baselines import WebStep, export_prompts

    answer = FreeTextAnswer(judgment_form="conditional", legal_path=None, conclusion="视人数而定",
                            citations=[{"law_title": "促进和规范数据跨境流动规定", "article_no": "第八条"}])
    ctx = ArmContext(chunks_path=tmp_path / "c")
    direct = DirectArm(model_id="m", node=_Node([answer])).run(scenario(), ctx)
    assert direct.status == "succeeded" and direct.citations[0].title == "促进和规范数据跨境流动规定"

    web = WebAgentArm(model_id="m", node=_Node([WebStep(action="search", query="门槛"),
                                                WebStep(action="answer", answer=answer)]),
                      search_client=_Search()).run(scenario(), ctx)
    assert web.status == "succeeded" and web.counters == {"web_searches": 1}

    (tmp_path / "imported").mkdir()
    (tmp_path / "imported" / "t_001.json").write_text(answer.model_dump_json(), encoding="utf-8")
    assert ImportedArm(name="manual", directory=tmp_path / "imported").run(scenario(), ctx).status == "succeeded"
    assert ImportedArm(name="manual", directory=tmp_path / "none").run(scenario(), ctx).status == "failed"
    assert export_prompts([scenario()], tmp_path / "prompts") == 1


def test_run_store_records_evidence_and_report_reads_it(tmp_path) -> None:
    answer = FreeTextAnswer(judgment_form="definite", legal_path="标准合同", conclusion="签标准合同",
                            citations=[{"law_title": "促进和规范数据跨境流动规定", "article_no": "第八条"}])
    chunks = tmp_path / "corpus" / "chunks.jsonl"
    chunks.parent.mkdir()
    chunks.write_text("", encoding="utf-8")
    store = RunStore(tmp_path / "run")
    judge = lambda s, d: JudgeVerdict(judgment_form="definite", legal_correctness="pass", overall_pass=True)
    grades = run_agent_eval([scenario()], [DirectArm(model_id="m", node=_Node([answer]))], store=store,
                            chunks_path=chunks, judge=judge, judge_model="j")
    assert [g.legal for g in grades] == ["wrong_definite"]  # FLOW unresolved without a manifest: required source missing
    assert (store.root / "direct" / "t_001" / "grade.json").exists()
    manifest = json.loads((store.root / "manifest.json").read_text(encoding="utf-8"))
    data, markdown = agent_report(grades, [scenario()], manifest)
    assert "危险错误" in markdown and data["groups"]["split=dev"]["direct"]["n"] == 1
    with pytest.raises(FileExistsError):
        RunStore(tmp_path / "run")


def test_grade_delivery_resolves_titles_against_sources() -> None:
    delivery = Delivery(arm="direct", scenario_id="t_001", status="succeeded", judgment_form="definite",
                        citations=[CitedSource(title="促进和规范数据跨境流动规定", article_no="第八条")])
    _, grade = grade_delivery(scenario(), delivery, SOURCES, None)
    assert grade.deterministic.passed and grade.legal == "unevaluated"


# --------------------------------------------------------------------------- retrieval


def test_retrieval_scoring_counts_articles_and_excluded_hits() -> None:
    labelled = scenario(retrieval={
        "queries": ["门槛"],
        "must_cite": [{"source_id": FLOW, "article_no": "第八条", "note": "n"}],
        "must_not_cite": [{"source_id": "old", "article_no": "第四条", "note": "n", "reason": "version_superseded"}],
    })

    def hit(source, article, rank, cite=True):
        return RetrievalHit(chunk_id=f"{source}:{article}", doc_id=source, source_id=source, title="t", text="x",
                            article_no=article, source_url="https://x", retriever="hybrid",
                            rank=rank, score=1.0, can_cite_clause=cite, citation_role="primary_legal_basis")

    hits = [hit("old", "第四条", 0), *[hit("noise", str(i), i + 1) for i in range(5)], hit(FLOW, "第八条", 6)]
    row = score_scenario(labelled, hits)
    assert row.must_cite_recall == {5: 0.0, 10: 1.0} and row.excluded_hits[5] == ["old 第四条"]
    summary = aggregate([row])
    assert summary["overall"]["must_cite_recall@10"] == 1.0
    assert "条款召回" in retrieval_report(summary, {})


def test_wilson_interval_is_honest_for_small_samples() -> None:
    assert wilson_interval(0, 0) is None
    low, high = wilson_interval(3, 3)
    assert high == 1.0 and low < 0.5


def test_cli_check_and_review_package(tmp_path, capsys) -> None:
    from law_agent.evals.cli import main

    assert main(["check", "--no-corpus"]) == 0
    assert "scenarios=" in capsys.readouterr().out
    assert main(["review-package", "--split", "dev", "--output-dir", str(tmp_path / "pkg")]) == 0
    assert (tmp_path / "pkg" / "review.md").exists()
    shutil.rmtree(tmp_path / "pkg")
    assert Path("law_agent/evals/bank/data/personas.json").exists()
