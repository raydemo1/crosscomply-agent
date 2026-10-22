"""Entry-specific evaluations must not force Q&A into review contracts."""

import json

import pytest

from law_agent.review.evalset.agent_schemas import JudgeVerdict
from law_agent.review.evalset.delivery_cases import (
    classified_cases,
    get_qa_cases,
    get_review_cases,
)
from law_agent.review.evalset.qa_runner import QA_JUDGE_VERSION, QAAnswerJudge, run_qa_case


def test_every_candidate_has_one_primary_entry_and_qa_keeps_source_identity():
    rows = classified_cases()
    assert len(rows) == 22 and sum(r["primary_entry"] == "qa" for r in rows) == 12
    assert sum(r["primary_entry"] == "review" for r in rows) == 10
    assert len({r["case_id"] for r in rows}) == len(rows)
    assert {c.case_id for c in get_qa_cases()} == {r["case_id"] for r in rows if r["primary_entry"] == "qa"}
    assert all(r["review_status"] == "candidate" for r in rows)
    assert len(get_review_cases()) == 10




def test_question_with_unknowns_does_not_need_formal_risk_fields_or_confirmation():
    case = next(c for c in get_qa_cases() if c.case_id == "eval_shanghai_001")
    def execute(conversation, run, *, checkpoint, **kwargs):
        assert "冻结" not in conversation["materials"][0]["parsed_text"]
        checkpoint(answer="先核对对应清单；是否满足条件还需确认业务信息。", status="completed",
                   result={"citations": [], "material_citations": [], "conditions": ["确认业务范围"]})
    result = run_qa_case(case, execute=execute)
    assert result["deterministic_pass"] and result["judge"] is None
    assert result["run"]["result"]["conditions"]


def test_qa_evaluation_rejects_review_delivery_fields():
    def execute(conversation, run, *, checkpoint, **kwargs):
        checkpoint(answer="答案", status="completed", result={"review_result": {}, "risk_level": "low"})
    result = run_qa_case(get_qa_cases()[0], execute=execute)
    assert result["checks"]["qa_contract"] is False
    assert result["deterministic_pass"] is False


def test_qa_evaluation_rejects_raw_tool_answer_before_judging():
    def execute(conversation, run, *, checkpoint, **kwargs):
        checkpoint(answer='<｜｜DSML｜｜ calls><｜｜DSML｜｜ invoke name="finish">',
                   status="completed", result={"citations": []})

    result = run_qa_case(get_qa_cases()[0], execute=execute,
                        judge=lambda *_: pytest.fail("工具调用格式不能交给法律评测冒充答案"))
    assert result["checks"]["plain_answer"] is False
    assert result["deterministic_pass"] is False and result["judge"] is None


@pytest.mark.parametrize("error", [False, True])
def test_qa_judge_receives_abstention_and_full_rubric_and_records_its_version(error):
    case = next(c for c in get_qa_cases() if c.case_id == "eval_out_of_corpus_001")
    result = {"answer": "未取得规则，不能确定义务。", "citations": [], "conditions": []}
    received = []

    class Node:
        def run(self, messages):
            received.extend(messages)
            if error:
                raise RuntimeError("unavailable")
            return JudgeVerdict(legal_correctness="pass", overall_pass=True)

    judge = QAAnswerJudge.__new__(QAAnswerJudge)
    judge.node, judge.model = Node(), "test-model"
    verdict = judge(case, result)
    payload = json.loads(received[1].content)
    assert payload["rubric"]["should_abstain"] is True
    assert payload["rubric"]["notes"] == case.rubric.notes
    assert payload["rubric"]["forbidden_judgments"] == case.rubric.forbidden_judgments
    assert payload["reference_basis"] == case.reference_basis
    assert payload["user_context"] == case.intake and payload["answer"] == result
    assert verdict["prompt_version"] == QA_JUDGE_VERSION
    assert verdict["overall_pass"] is (None if error else True)
    assert verdict["error"] == ("RuntimeError" if error else None)
