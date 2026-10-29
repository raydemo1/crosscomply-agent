"""Single-source model submissions and governed report assembly."""

import json

import pytest

from law_agent.review.agent import AgentModel, AgentState, AgentStep, revision_targets, run_agent
from law_agent.review.remediation import guided_actions_from_review
from law_agent.review.result_builder import LLMReviewResultDraft, WebImpactDraft
from law_agent.review.schemas import GroundedClaimDraft
from law_agent.review.semantic_grounding import SemanticGroundingVerifier, SemanticVerdict
from law_agent.review.web_research import WebFinding
from tests.test_review_completion_contracts import draft, tools
from tests.test_review_llm import FakeClient, tool_message


def test_production_finish_exposes_only_model_owned_fields():
    model = AgentModel(model_id="test", client=FakeClient())
    schema = next(t["function"]["parameters"] for t in model.protocol.tools
                  if t["function"]["name"] == "finish")
    definitions = schema["$defs"]
    fields = definitions["LLMReviewResultDraft"]["properties"]
    assert {"questions", "web_impacts"} <= fields.keys()
    assert not {"missing_information", "missing_answer_types", "web_impact", "material_web_urls"} & fields.keys()
    issue = definitions["ReviewIssueDraft"]
    assert "question_indexes" in issue["properties"]
    assert not {"unknowns", "answer_type"} & issue["properties"].keys()
    assert not {"material_evidence", "supporting_chunk_ids"} & set(issue["required"])
    assert "supporting_citation_refs" not in definitions["GroundedClaimDraft"]["properties"]


@pytest.mark.parametrize("indexes", [[-1], [True], ["0"], [1], [0, 0], []])
def test_question_references_reject_invalid_or_missing_indexes(indexes):
    with pytest.raises(ValueError):
        draft(questions=[{"text": "实际人数"}], issues=[{
            "kind": "missing_information", "title": "人数未知", "finding": "需确认实际人数",
            "question_indexes": indexes, "recommended_action": "确认人数",
        }])


@pytest.mark.parametrize("questions,gaps", [
    ([{"text": "  "}], []),
    ([{"text": "人数"}, {"text": " 人数 "}], []),
    ([{"text": "缺官方解释"}], ["缺官方解释"]),
])
def test_question_list_rejects_blank_duplicate_and_legal_questions(questions, gaps):
    with pytest.raises(ValueError):
        draft(questions=questions, legal_basis_gaps=gaps)


def test_single_question_list_drives_native_finish_report_and_unique_tasks():
    submission = draft(questions=[
        {"text": "实际累计人数", "answer_type": "count"},
        {"text": "是否属于CIIO", "answer_type": "choice"},
    ], issues=[
        {"kind": "missing_information", "title": title, "finding": "关键事实待确认",
         "question_indexes": indexes, "recommended_action": "补充事实"}
        for title, indexes in [("人数与身份", [0, 1]), ("人数口径", [0])]
    ])
    client = FakeClient(outputs=[tool_message(("finish", "finish", {
        "summary": "提交条件性结论", "draft": submission.model_dump(mode="json"),
    }))])
    finalizer = tools()
    seen = []

    def verify(**kwargs):
        seen.append(kwargs["draft"].model_dump(mode="json"))
        return SemanticVerdict(status="supported", claim_checks=[], conclusion_reason="条件性报告")

    finalizer._semantic_verifier = verify
    state = run_agent(AgentState(goal="审查"), material="", intake={},
        decide=AgentModel(model_id="test", client=client), search=lambda *_, **_kwargs: [], web_search=lambda *_: [],
        finalize=lambda report, current: finalizer.finalize(report, current, case_id="case", intake_snapshot={"facts": {}}),
        checkpoint=lambda _: None)
    report = state.result["review_result"]
    assert state.status == "completed" and len(client.calls) == len(seen) == 1
    assert report["missing_information"] == ["实际累计人数", "是否属于CIIO"]
    assert report["missing_answer_types"] == {"实际累计人数": "count", "是否属于CIIO": "choice"}
    assert report["issues"][0]["unknowns"] == report["missing_information"]
    assert report["issues"][1]["unknowns"] == ["实际累计人数"]
    assert report["issues"][1]["answer_type"] == "count"
    actions = guided_actions_from_review(report, requester_id="applicant")
    assert [(a["title"], a["answer_type"]) for a in actions] == [
        ("实际累计人数", "count"), ("是否属于CIIO", "choice"),
    ]
    assert "missing_information" not in seen[0]


@pytest.mark.parametrize("impacts,expected", [
    ([], "none"), (["supplement"], "supplement"),
    (["supplement", "execution_detail"], "execution_detail"),
    (["execution_detail", "core"], "core"),
])
def test_web_entries_generate_one_consistent_summary(impacts, expected):
    entries = [{"url": f"https://www.gov.cn/guide/{i}", "impact": impact}
               for i, impact in enumerate(impacts)]
    submission = draft(web_impacts=entries)
    state = AgentState(goal="审查", steps=[AgentStep(number=1, action="search_web", summary="调查",
        observation={"findings": [WebFinding(title="官方来源", url=item["url"]).model_dump(mode="json")
                                  for item in entries]})])
    result = tools().finalize(submission, state, case_id="case", intake_snapshot={"facts": {}}, system_abstention=True)
    assert result["web_impact"] == expected
    assert result["material_web_urls"] == [item["url"] for item in entries]
    assert result["freshness_hold"] is (expected == "core")
    assert "web_impact" not in submission.model_dump()


def test_web_entries_reject_duplicate_canonical_urls_and_unpaired_values():
    with pytest.raises(ValueError, match="重复"):
        draft(web_impacts=[
            {"url": "http://www.gov.cn/guide/", "impact": "supplement"},
            {"url": "https://gov.cn/guide#part", "impact": "core"},
        ])
    for entry in [{"impact": "core"}, {"url": "https://www.gov.cn/guide"}]:
        with pytest.raises(ValueError):
            WebImpactDraft.model_validate(entry)


def test_verifier_and_revision_feedback_locate_the_actual_question_field():
    submission = draft(questions=[{"text": "预计明年人数"}])
    client = FakeClient(outputs=[{
        "status": "unsupported", "claim_checks": [], "conclusion_reason": "不是本次阻塞事实",
        "core_checks": [{"target": "questions:0:text", "status": "unsupported",
                         "reason": "需确认实际累计人数，不是未来预测"}],
    }])
    verdict = SemanticGroundingVerifier(model_id="test", client=client)(
        review_goal="当前路径", draft=submission, confirmed_intake={}, extracted_facts=AgentState(goal="审查").facts,
        material="", evidence=[])
    payload = json.loads(client.calls[0][1].content)
    assert "questions:0:text" in payload["core_targets"]
    assert not any(t.startswith("missing_information:") for t in payload["core_targets"])
    target = revision_targets(submission, verdict, evidence_ids=set())[0]
    assert target["target"] == "questions:0:text" and target["text"] == "预计明年人数"


def test_model_claim_cannot_supply_program_generated_citation_numbers():
    with pytest.raises(ValueError):
        GroundedClaimDraft.model_validate({
            "text": "法律主张", "supporting_chunk_ids": ["c1"], "supporting_citation_refs": ["伪造编号"],
        })




def test_core_web_impact_does_not_silently_rewrite_a_determined_conclusion():
    report = draft().model_dump(mode="json") | {
        "risk_level": "medium", "legal_path": "确定路径",
        "claims": [{"text": "确定法律判断", "supporting_chunk_ids": ["c1"]}],
        "web_impacts": [{"url": "https://www.gov.cn/new", "impact": "core"}],
    }
    submission = LLMReviewResultDraft.model_validate(report)
    state = AgentState(goal="审查", steps=[AgentStep(number=1, action="search_web", summary="新来源",
        observation={"findings": [WebFinding(title="新解释", url="https://www.gov.cn/new").model_dump(mode="json")]})])
    with pytest.raises(ValueError, match="暂缓确定结论"):
        tools().finalize(submission, state, case_id="case", intake_snapshot={"facts": {}})
    assert submission.risk_level == "medium" and submission.legal_path == "确定路径"
