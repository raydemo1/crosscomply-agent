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
    ArticleRef,
    MaterialFile,
    RetrievalLabels,
    Scenario,
    ScriptedAnswer,
    scenario_hash,
)
from law_agent.evals.delivery import CitedSource, Delivery, FreeTextAnswer
from law_agent.evals.grading import JudgeVerdict, classify, deterministic_grade, resolve_citations
from law_agent.evals.report import agent_report, retrieval_report
from law_agent.evals.retrieval import SearchOutcome, aggregate, score_scenario
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
        RetrievalLabels(queries=[{"text": "q", "targets": ["s"]}], must_cite=[{"source_id": "s", "note": "n"}])
    with pytest.raises(ValidationError, match="没有对准"):
        RetrievalLabels(queries=[{"text": "q"}], must_cite=[{"source_id": "s", "article_no": "第一条", "note": "n"}])
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


def test_retrieval_scoring_separates_source_article_navigation_and_query_rank() -> None:
    labelled = scenario(retrieval={
        "queries": [{"text": "门槛", "targets": [f"{FLOW} 第八条", f"{FLOW} 第七条"]}],
        "must_cite": [{"source_id": FLOW, "article_no": "第八条", "note": "n"},
                      {"source_id": FLOW, "article_no": "第七条", "note": "n"}],
        "must_not_cite": [{"source_id": "old", "article_no": "第四条", "note": "n", "reason": "version_superseded"}],
    })

    def hit(source, article, rank, cite=True):
        return RetrievalHit(chunk_id=f"{source}:{article}", doc_id=source, source_id=source, title="t", text="x",
                            article_no=article, source_url="https://x", retriever="hybrid",
                            rank=rank, score=1.0, can_cite_clause=cite, citation_role="primary_legal_basis")

    bodies = [hit("old", "第四条", 0), hit(FLOW, "第八条", 1)]
    listed = [{"source_id": FLOW, "locations": [{"article_no": "第七条"}]}]
    ranked = [*bodies, *[hit("noise", str(i), i) for i in range(5)], hit(FLOW, "第七条", 9)]
    row = score_scenario(labelled, SearchOutcome(hits=bodies, results=listed, per_query=[ranked]))
    by_key = {label.key: label for label in row.labels}
    assert by_key[f"{FLOW} 第八条"].article and by_key[f"{FLOW} 第七条"].source
    assert not by_key[f"{FLOW} 第七条"].article and by_key[f"{FLOW} 第七条"].discoverable
    assert [t.rank for t in row.query_targets] == [2, 8] and row.excluded_hits == ["old 第四条"]
    summary = aggregate([row])["overall"]
    assert summary["must_cite_article"] == 0.5 and summary["must_cite_discoverable"] == 1.0
    assert summary["query_target@3"] == 0.5 and summary["query_target@8"] == 1.0
    assert "条款召回" in retrieval_report(aggregate([row]), {})


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


# --------------------------------------------------------------------------- qa / evidence recall


def test_qa_eval_scores_gathered_evidence_and_citations_with_the_shared_scorer() -> None:
    from law_agent.evals.qa_eval import aggregate as qa_aggregate
    from law_agent.evals.qa_eval import qa_report, run_scenario

    labelled = scenario(retrieval={
        "queries": [{"text": "门槛", "targets": [f"{FLOW} 第八条", f"{FLOW} 第七条"]}],
        "must_cite": [{"source_id": FLOW, "article_no": "第八条", "note": "n"},
                      {"source_id": FLOW, "article_no": "第七条", "note": "n"}],
        "must_not_cite": [{"source_id": "old", "article_no": "第四条", "note": "n", "reason": "version_superseded"}],
    })
    seen = {}

    def fake_execute(conversation, run, *, checkpoint, chunks_path, current_date):
        seen.update(current_date=current_date, materials=len(conversation["materials"]))
        checkpoint(state={"evidence": [{"chunk_id": "c8", "source_id": FLOW, "article_no": "第八条"},
                                       {"chunk_id": "c7", "source_id": FLOW, "article_no": "第七条"}],
                          "searches": 2, "reads": 1, "turns": 4,
                          "queries": [{"text": "门槛", "scope": "全国通用"}, {"text": "北京临床", "scope": "地区:北京"}]})
        checkpoint(result={"answer": "答", "citations": [
            {"source_id": FLOW, "article_no": "第八条", "can_cite_clause": True},
            {"source_id": "old", "article_no": "第四条", "can_cite_clause": True}]})

    run = run_scenario(labelled, chunks_path=Path("unused"), execute=fake_execute)
    assert seen["current_date"] == labelled.as_of_date and seen["materials"] == len(labelled.materials)
    assert run.status == "completed" and run.searches == 2
    assert run.query_scopes == ["全国通用", "地区:北京"]
    assert [(label.retrieved, label.cited) for label in run.evidence.labels] == [(True, True), (True, False)]
    assert run.evidence.excluded_citations == ["old 第四条"]
    summary = qa_aggregate([run])["overall"]
    assert summary["must_cite_retrieved"] == 1.0 and summary["must_cite_cited"] == 0.5
    assert summary["scope_counts"] == {"全国通用": 1, "地区:北京": 1}
    report = qa_report(qa_aggregate([run]), {})
    assert "证据召回" in report and "检索域选择分布" in report and "| 地区:北京 | 1 |" in report


def test_qa_eval_scores_the_draft_before_the_completeness_review_on_the_same_run() -> None:
    from law_agent.evals.qa_eval import aggregate as qa_aggregate
    from law_agent.evals.qa_eval import qa_report, run_scenario

    labelled = scenario(retrieval={
        "queries": [{"text": "门槛", "targets": [f"{FLOW} 第八条", f"{FLOW} 第七条"]}],
        "must_cite": [{"source_id": FLOW, "article_no": "第八条", "note": "n"},
                      {"source_id": FLOW, "article_no": "第七条", "note": "n"}],
    }, scripted_answers=[{"answer": "人数已确认"}])

    def fake_execute(conversation, run, *, checkpoint, chunks_path, current_date):
        if run["id"] == "r1":
            checkpoint(state={"evidence": [{"chunk_id": "c8", "source_id": FLOW, "article_no": "第八条"}]},
                       result={"questions": [{"question": "人数？"}]})
            return
        checkpoint(state={"evidence": [{"chunk_id": "c7", "source_id": FLOW, "article_no": "第七条"}],
                          "searches": 2, "reads": 0, "turns": 3, "queries": [{"text": "门槛"}]})
        checkpoint(result={"answer": "修订", "citations": [
            {"source_id": FLOW, "article_no": "第八条", "can_cite_clause": True},
            {"source_id": FLOW, "article_no": "第七条", "can_cite_clause": True}],
            "completeness": {"checked": True, "gaps": [{"issue": "缺第七条", "query": "申报评估"}],
                             "draft_evidence_ids": [], "draft_answer": "草稿",
                             "draft_citations": [{"source_id": FLOW, "article_no": "第八条", "can_cite_clause": True}]}})

    run = run_scenario(labelled, chunks_path=Path("unused"), execute=fake_execute)
    assert [(label.retrieved, label.cited) for label in run.draft_evidence.labels] == [(True, True), (False, False)]
    assert [(label.retrieved, label.cited) for label in run.evidence.labels] == [(True, True), (True, True)]
    assert run.draft_answer == "草稿"
    failed = run.model_copy(update={"completeness_status": "failed"})
    skipped = run.model_copy(update={"completeness_status": "skipped"})
    summary = qa_aggregate([run, failed, skipped])["overall"]
    assert summary["review_outcomes"] == {"completed": 1, "skipped": 1, "failed": 1}
    assert summary["before_review"]["scored_runs"] == 1
    assert summary["before_review"]["must_cite_cited"] == 0.5 and summary["after_review"]["must_cite_cited"] == 1.0
    assert summary["review_gap_runs"] == 1 and "完整性复核前后" in qa_report(qa_aggregate([run]), {})

    # A label change rescores the saved run without calling the Agent again.
    from law_agent.evals.qa_eval import QARun, rescore

    relabelled = labelled.model_copy(deep=True)
    relabelled.retrieval.must_cite[1].equivalents = [[ArticleRef(source_id="old", article_no="第四条")]]
    saved = QARun.model_validate_json(run.model_dump_json())
    assert [label.cited for label in rescore(saved, relabelled).evidence.labels] == [True, True]


def test_qa_eval_records_a_crash_without_stopping() -> None:
    from law_agent.evals.qa_eval import run_scenario

    labelled = scenario(retrieval={"queries": [{"text": "q", "targets": [f"{FLOW} 第八条"]}],
                                   "must_cite": [{"source_id": FLOW, "article_no": "第八条", "note": "n"}]})

    def boom(*_args, **_kwargs):
        raise RuntimeError("upstream 502")

    run = run_scenario(labelled, chunks_path=Path("unused"), execute=boom)
    assert run.status == "failed" and "502" in run.error


def test_equivalent_articles_satisfy_a_label_only_as_a_whole_group() -> None:
    from law_agent.evals.bank.schema import RetrievalLabels
    from law_agent.evals.evidence import score_evidence

    pipl = "flk_npc_ff8081817b6472a3017b656cc2040044"
    labels = RetrievalLabels.model_validate({
        "queries": [{"text": "义务", "targets": [f"{FLOW} 第十条"]}],
        "must_cite": [{"source_id": FLOW, "article_no": "第十条", "note": "告知、单独同意、影响评估",
                       "equivalents": [[{"source_id": pipl, "article_no": "第三十九条"},
                                        {"source_id": pipl, "article_no": "第五十五条"}]]}],
    })
    both = [{"source_id": pipl, "article_no": "第三十九条"}, {"source_id": pipl, "article_no": "第五十五条"}]
    score = score_evidence(labels, evidence=both[:1], citations=both)
    assert [(label.retrieved, label.cited) for label in score.labels] == [(False, True)]

    with pytest.raises(ValidationError, match="等价条款"):
        RetrievalLabels.model_validate({
            "queries": [{"text": "q", "targets": [f"{FLOW} 第十条"]}],
            "must_cite": [{"source_id": FLOW, "article_no": "第十条", "note": "n"}],
            "optional": [{"source_id": pipl, "article_no": "第三十九条", "note": "n",
                          "equivalents": [[{"source_id": pipl, "article_no": "第五十五条"}]]}],
        })


def test_qa_eval_records_where_each_read_came_from() -> None:
    from law_agent.evals.qa_eval import aggregate as qa_aggregate
    from law_agent.evals.qa_eval import qa_report, run_scenario

    labelled = scenario(retrieval={"queries": [{"text": "q", "targets": [f"{FLOW} 第八条"]}],
                                   "must_cite": [{"source_id": FLOW, "article_no": "第八条", "note": "n"}]})

    def read(source, article=None, chunk=None, offset=0, returned=()):
        return {"read_request": {"source_id": source, "article_no": article, "chunk_id": chunk, "offset": offset},
                "resolved_source_id": source, "chunk_ids": list(returned)}

    steps = [
        {"action": "search_evidence", "observation": {
            "chunk_ids": ["old4"],
            "results": [{"source_id": FLOW, "locations": [{"chunk_id": "f8", "article_no": "第八条", "preview": "p"}]},
                        {"source_id": "old", "locations": [{"chunk_id": "old4", "article_no": "第四条", "acquired": True}]}]}},
        {"action": "read_evidence", "observation": {"read_results": [
            read(FLOW, "第八条", returned=["f8"]),          # listed with a preview
            read(FLOW, "第十三条", returned=["f13"]),       # shown with the old source
            read("old", "第五条", returned=["old5"]),        # another article of a returned source
            read("old", "第五条", offset=5),                 # paging
            read("pipl", "第三十九条"),                       # named by the Agent itself
        ]}},
    ]
    evidence = [{"chunk_id": "old4", "source_id": "old", "article_no": "第四条",
                 "later_provisions": [{"source_id": FLOW, "article_no": "第十三条"}]},
                {"chunk_id": "f8", "source_id": FLOW, "article_no": "第八条"},
                {"chunk_id": "f13", "source_id": FLOW, "article_no": "第十三条"},
                {"chunk_id": "old5", "source_id": "old", "article_no": "第五条"}]

    def fake_execute(conversation, run, *, checkpoint, chunks_path, current_date):
        checkpoint(state={"evidence": evidence, "steps": steps, "searches": 1, "reads": 5, "turns": 3})
        checkpoint(result={"answer": "答", "citations": []})

    run = run_scenario(labelled, chunks_path=Path("unused"), execute=fake_execute)
    assert [r.origin for r in run.read_records] == [
        "preview", "later_provision", "returned_source", "continuation", "direct"]
    origins = qa_aggregate([run])["overall"]["read_origins"]
    assert origins == {"preview": 1, "later_provision": 1, "returned_source": 1, "continuation": 1, "direct": 1}
    assert "来自检索结果的预览 | 1" in qa_report(qa_aggregate([run]), {})


def test_query_replay_ranks_each_label_at_its_best_query_and_groups_at_their_worst_member(tmp_path) -> None:
    from law_agent.evals.query_replay import QuerySet, export_queries, replay_report, run_replay

    pipl = "flk_npc_ff8081817b6472a3017b656cc2040044"
    labelled = scenario(retrieval={
        "queries": [{"text": "q", "targets": [f"{FLOW} 第八条", f"{FLOW} 第十条"]}],
        "must_cite": [{"source_id": FLOW, "article_no": "第八条", "note": "n"},
                      {"source_id": FLOW, "article_no": "第十条", "note": "n", "equivalents": [[
                          {"source_id": pipl, "article_no": "第三十九条"},
                          {"source_id": pipl, "article_no": "第五十五条"}]]}],
    })

    def hit(source, article):
        return RetrievalHit(chunk_id=f"{source}:{article}", doc_id=source, source_id=source, title="t", text="x",
                            article_no=article, source_url="https://x", retriever="hybrid", rank=0, score=1.0,
                            can_cite_clause=True, citation_role="primary_legal_basis")

    noise = [hit("noise", str(i)) for i in range(10)]
    rankings = {"甲": [*noise[:5], hit(FLOW, "第八条"), hit(pipl, "第三十九条")],   # 第八条 6th, 39 7th
                "乙": [hit(FLOW, "第八条"), *noise[:8], hit(pipl, "第五十五条")]}  # 第八条 1st, 55 10th

    results, summary = run_replay([labelled], QuerySet(source_run="r", base_commit="c", queries={
        labelled.scenario_id: ["甲", "乙"]}), lambda _s, texts: [rankings[t] for t in texts])
    assert [(label.rank, label.query) for label in results[0].labels] == [(1, "乙"), (10, "乙")]
    assert summary["must_cite@3"] == 0.5 and summary["must_cite@12"] == 1.0
    assert "前8名" in replay_report(summary, {}, QuerySet(source_run="r", base_commit="c", queries={}))

    run_dir = tmp_path / "qa_dev_x"
    (run_dir / "qa" / "s1").mkdir(parents=True)
    (run_dir / "manifest.json").write_text('{"base_commit": "abc"}', encoding="utf-8")
    (run_dir / "qa" / "s1" / "result.json").write_text(json.dumps(
        {"scenario_id": "s1", "split": "dev", "queries": ["甲", "甲", "乙"],
         "query_scopes": ["全国通用", "全国通用", "地区:北京"]}), encoding="utf-8")
    exported = export_queries(run_dir)
    assert exported.queries == {"s1": ["甲", "乙"]}
    assert exported.query_scopes == {"s1": ["全国通用", "地区:北京"]}
    seen_scopes = []
    run_replay([labelled], QuerySet(source_run="r", base_commit="c",
                                    queries={labelled.scenario_id: ["甲", "乙"]},
                                    query_scopes={labelled.scenario_id: ["全国通用", "地区:北京"]}),
               lambda _s, texts, scopes=None: (seen_scopes.append(scopes), [rankings[t] for t in texts])[1])
    assert seen_scopes == [["全国通用", "地区:北京"]]
    (run_dir / "qa" / "s1" / "result.json").write_text(json.dumps(
        {"scenario_id": "s1", "split": "test", "queries": ["甲"]}), encoding="utf-8")
    with pytest.raises(ValueError, match="test"):
        export_queries(run_dir)
