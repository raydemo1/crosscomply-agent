"""The Agent receives legal entailment feedback and abstains when its budget ends."""

import json
from datetime import date

import pytest

from law_agent.data.schemas import Chunk
from law_agent.review.agent import AgentDecision, AgentState, revision_targets, run_agent
from law_agent.review.llm import ReviewWorkflowFailed
from law_agent.review.report_grounding import ReportCheck
from law_agent.review.result_builder import (
    LLMReviewResultDraft,
    ReviewIssueDraft,
    ReviewQuestionDraft,
)
from law_agent.review.schemas import GroundedClaim, ReviewFacts
from law_agent.review.semantic_grounding import (
    SemanticGroundingRejected,
    SemanticGroundingVerifier,
    SemanticVerdict,
)
from tests.test_review_llm import FakeClient
from tests.test_review_result_builder import _hit


def _verify(client, draft=None, **kwargs):
    return SemanticGroundingVerifier(model_id="test", client=client)(
        review_goal="当前审查目标", draft=draft or _draft(), confirmed_intake={},
        extracted_facts=ReviewFacts(), material="冻结材料", evidence=[_hit()], **kwargs,
    )


def test_production_verifier_request_meets_json_object_message_requirement(monkeypatch):
    from law_agent.config import LLMConfig
    from law_agent.llm.openai_compatible import OpenAICompatibleClient

    config = LLMConfig(base_url="https://api.deepseek.com", beta_base_url="https://api.deepseek.com/beta",
                       api_key="fake", model="test", timeout_seconds=60,
                       structured_output_mode="json_object", reasoning_effort="low")
    requests = []
    def receive(payload, _config, **_kwargs):
        requests.append(payload)
        assert payload["response_format"] == {"type": "json_object"}
        assert any("json" in str(message["content"]).lower() for message in payload["messages"])
        return _core_supported()
    monkeypatch.setattr("law_agent.llm.openai_compatible.receive_json_response", receive)
    assert _verify(OpenAICompatibleClient(config)).status == "supported"
    assert len(requests) == 1


def test_single_request_checks_issue_as_a_whole_and_locates_only_the_problem():
    issue = ReviewIssueDraft(
        kind="legal_gap", title="条件待确认", finding="决定性事实未知", question_indexes=[0],
        material_evidence=[], supporting_chunk_ids=["c1"], recommended_action="确认事实后判断",
    )
    draft = _draft().model_copy(update={"questions": [ReviewQuestionDraft(text="条件是否成立")], "issues": [issue], "risk_boundaries": ["条件变化后重新判断"]})
    client = FakeClient(outputs=[{**_core_supported(), "report_checks": [{
        "target": "issues:0:kind", "status": "uncertain", "reason": "未知事实不能认定已成立的问题",
        "evidence_ids": ["c1"], "missing_facts": ["条件是否成立"],
    }]}])
    verdict = _verify(client, draft)
    assert len(client.calls) == 1
    payload = json.loads(client.calls[0][1].content)
    assert payload["report_background"] == draft.model_dump(mode="json")
    assert {"issues:0:kind", "issues:0:finding", "issues:0:question_indexes:0", "issues:0:recommended_action",
            "risk_boundaries:0", "recommended_actions:0"} <= set(payload["report_targets"])
    assert payload["report_schema"]["$defs"]["ReviewIssueDraft"]["properties"]["kind"]["description"]
    assert client.kwargs[0]["stream"] is True
    assert verdict.status == "uncertain" and verdict.missing_facts == ["条件是否成立"]
    assert revision_targets(draft, verdict, evidence_ids={"c1"})[0]["target"] == "issues:0:kind"


def test_core_and_boundary_errors_are_returned_together_in_one_request():
    draft = _draft().model_copy(update={"risk_boundaries": ["只有人数改变就一律需要申报"]})
    client = FakeClient(outputs=[{**_core_supported(),
        "core_checks": [{"target": "conclusion", "status": "uncertain", "reason": "缺例外衔接",
                         "missing_basis": "两项规定的适用关系"}],
        "report_checks": [{"target": "risk_boundaries:0", "status": "unsupported",
                           "reason": "人数变化不排除例外", "evidence_ids": ["c1"]}],
    }])
    verdict = _verify(client, draft)
    assert verdict.status == "unsupported" and len(client.calls) == 1
    assert verdict.missing_facts == []
    targets = revision_targets(draft, verdict, evidence_ids={"c1"})
    assert {t["target"] for t in targets} == {"conclusion", "risk_boundaries:0"}
    assert next(t for t in targets if t["target"] == "conclusion")["missing_basis"]


def test_supported_report_does_not_require_checks_for_every_advice_or_boundary():
    draft = _draft().model_copy(update={"risk_boundaries": ["范围变化时重新核对"]})
    client = FakeClient(outputs=[_core_supported()])
    verdict = _verify(client, draft)
    assert verdict.status == "supported" and verdict.report_checks == []
    assert len(client.calls) == 1


def test_claim_revision_feedback_preserves_its_source_bindings_and_typed_facts():
    verdict = SemanticVerdict.model_validate({
        **_core_supported(), "claim_checks": [{"claim_index": 0, "status": "uncertain",
        "reason": "适用前提未知", "missing_facts": ["条件是否成立"]}],
    })
    target = revision_targets(_draft(), verdict, evidence_ids={"c1"})[0]
    assert target["target"] == "claims:0" and target["evidence_ids"] == ["c1"]
    assert target["missing_facts"] == ["条件是否成立"]


@pytest.mark.parametrize("patch", [
    {"claim_checks": []},
    {"claim_checks": [{"claim_index": 1, "status": "supported", "reason": "不存在"}]},
    {"claim_checks": [{"claim_index": 0, "status": "supported", "reason": "支持"}] * 2},
    {"core_checks": [{"target": "invented", "status": "uncertain", "reason": "不存在"}]},
    {"core_checks": [{"target": "recommended_actions:0", "status": "uncertain", "reason": "错误分组"}]},
    {"report_checks": [{"target": "recommended_actions:1", "status": "uncertain", "reason": "不存在"}]},
    {"report_checks": [{"target": "recommended_actions:0", "status": "supported", "reason": "应省略"}]},
    {"report_checks": [{"target": "recommended_actions:0", "status": "uncertain", "reason": "重复"}] * 2},
    {"report_checks": [{"target": "recommended_actions:0", "status": "uncertain", "reason": "伪造引用",
                        "evidence_ids": ["unknown"]}]},
    {"status": "unsupported"},
])
def test_invalid_feedback_fails_closed_after_one_structural_repair(patch):
    invalid = {**_core_supported(), **patch}
    client = FakeClient(outputs=[invalid, invalid])
    with pytest.raises(ReviewWorkflowFailed) as exc:
        _verify(client)
    assert exc.value.reason == "post_validation_failed" and len(client.calls) == 2


@pytest.mark.parametrize("quote", [None, "非逐字定位", "核查例外"])
def test_sparse_feedback_does_not_require_an_exact_quote(quote):
    client = FakeClient(outputs=[{**_core_supported(), "report_checks": [{
        "target": "recommended_actions:0", "status": "uncertain", "reason": "缺少规范衔接依据",
        "quote": quote, "evidence_ids": ["c1"], "missing_basis": "适用关系",
    }]}])
    verdict = _verify(client)
    target = revision_targets(_draft(), verdict, evidence_ids={"c1"})[0]
    assert verdict.status == "uncertain" and verdict.missing_facts == []
    assert target["evidence_ids"] == ["c1"]
    assert ("start_offset" in target) == (quote == "核查例外")


def test_full_report_preserves_review_date_web_identity_and_uncited_counterevidence(monkeypatch):
    from law_agent.review.web_research import WebFinding

    monkeypatch.setattr("law_agent.review.semantic_grounding.business_today", lambda: date(2026, 10, 22))
    law = _hit().model_copy(update={"chunk_id": "exception", "full_article_text": "完整例外原文",
                                    "effective_date": "2024-03-22"})
    web = WebFinding(title="公开解释", url="https://example.org/notes", excerpt="仅有摘录")
    client = FakeClient(outputs=[_core_supported()])
    SemanticGroundingVerifier(model_id="test", client=client)(
        review_goal="历史时点", draft=_draft(), confirmed_intake={}, extracted_facts=ReviewFacts(),
        material="冻结材料", evidence=[_hit(), law, law], as_of_date=date(2024, 4, 1), web_findings=[web],
    )
    payload = json.loads(client.calls[0][1].content)
    assert payload["current_date"] == "2026-10-22" and payload["review_date"] == "2024-04-01"
    assert payload["web_findings"] == [web.model_dump(mode="json")]
    assert [a["chunk_id"] for a in payload["context_authorities"]] == ["exception"]
    assert payload["context_authorities"][0]["article_text"] == "完整例外原文"
    assert payload["context_authorities"][0]["effective_date"] == "2024-03-22"


def _draft() -> LLMReviewResultDraft:
    return LLMReviewResultDraft(
        risk_level="medium",
        decision_summary="本案现有资料提示需要进一步核对数据出境路径，但已引用法条和确认事实暂时不足以排除全部法定免予情形。",
        legal_path="标准合同",
        conclusion="应采用标准合同。",
        claims=[GroundedClaim(text="不存在免予情形", supporting_chunk_ids=["c1"])],
        trigger_reasons=["人数超过某一数量门槛"],
        questions=[],
        recommended_actions=["核查例外"],
        risk_boundaries=[],
    )


def test_supported_verdict_cannot_determine_path_with_blocking_missing_facts():
    client = FakeClient(outputs=[{
        "status": "supported", "conclusion_reason": "正文给出条件分支",
        "claim_checks": [{"claim_index": 0, "status": "supported", "reason": "条文准确"}],
        "missing_facts": ["清单内外尚未确认"],
    }])
    verifier = SemanticGroundingVerifier(model_id="test-model", client=client)
    verdict = verifier(review_goal="能否出境", draft=_draft(), confirmed_intake={},
                       extracted_facts=ReviewFacts(), material="申请材料", evidence=[_hit()])
    assert verdict.status == "uncertain"
    assert "legal_path" in verdict.conclusion_reason
    assert len(client.calls) == 1


@pytest.mark.parametrize("status", ["uncertain", "unsupported"])
def test_unresolved_claim_cannot_be_overridden_by_supported_summary(status):
    verdict = SemanticVerdict(
        status="supported",
        claim_checks=[{"claim_index": 0, "status": status, "reason": "义务适用关系尚未证明"}],
        conclusion_reason="主路径正确",
    )
    assert verdict.status == status
    assert "claim:0" in verdict.conclusion_reason
    assert "义务适用关系" in verdict.conclusion_reason
    assert verdict.missing_facts == []


def test_verifier_receives_full_article_and_rejects_unresolved_exemptions() -> None:
    client = FakeClient(outputs=[{
        "status": "uncertain",
        "claim_checks": [{"claim_index": 0, "status": "uncertain", "reason": "并列免予情形未核查", "missing_facts": ["个人合同必要性"]}],
        "conclusion_reason": "第五条其他免予分支尚未核查",
        "missing_facts": ["个人合同必要性"],
    }])
    verifier = SemanticGroundingVerifier(model_id="test-model", client=client)
    hit = _hit().model_copy(update={"full_article_text": "第五条（一）个人合同；（四）数量条件。", "effective_date": "2024-03-22"})

    verdict = verifier(
        review_goal="只判断出境机制，不认定手续已经完成",
        draft=_draft(), confirmed_intake={"annual_non_sensitive_count": "300000", "count_period": "unknown"},
        extracted_facts=ReviewFacts(), material="申请材料", evidence=[hit],
    )

    payload = json.loads(client.calls[0][1].content)
    assert payload["review_goal"] == "只判断出境机制，不认定手续已经完成"
    assert payload["cited_authorities"][0]["effective_date"] == "2024-03-22"
    assert payload["cited_authorities"][0]["article_text"] == hit.full_article_text
    assert payload["confirmed_intake"]["count_period"] == "unknown"
    assert verdict.status == "uncertain"
    assert client.kwargs[0]["model"] == "test-model"
    assert len(client.calls) == 1


def test_verifier_sees_the_applicability_boundary_of_each_cited_authority() -> None:
    """检索到不等于适用于本案：适用地区与对象必须随被引法源一并核验。"""

    client = FakeClient(outputs=[{
        "status": "supported",
        "claim_checks": [{"claim_index": 0, "status": "supported", "reason": "支持"}],
        "conclusion_reason": "已核查",
    }])
    verifier = SemanticGroundingVerifier(model_id="test-model", client=client)
    chunk = Chunk(
        chunk_id="c1",
        doc_id="d1",
        source_id="s1",
        title="上海自贸区数据出境负面清单",
        text="再保险领域：需要通过数据出境安全评估的数据清单。",
        chunk_index=0,
        source_url="u",
        char_count=26,
        doc_type="guideline",
        citation_role="conditional_local_basis",
        applicable_region="CN-SH",
        applicable_subjects=["再保险"],
    )

    verifier(
        review_goal="判断上海自贸区规则是否适用",
        draft=_draft(), confirmed_intake={}, extracted_facts=ReviewFacts(),
        material="申请材料", evidence=[_hit()], chunks_by_id={"c1": chunk},
    )

    payload = json.loads(client.calls[0][1].content)
    assert payload["cited_authorities"][0]["applicable_region"] == "CN-SH"
    assert payload["cited_authorities"][0]["applicable_subjects"] == ["再保险"]


def test_verifier_receives_returned_legal_interpretations_without_granting_clause_authority():
    client = FakeClient(outputs=[{
        "status": "supported", "conclusion_reason": "依据法条及解释资料核对",
        "claim_checks": [{"claim_index": 0, "status": "supported", "reason": "支持"}],
    }])
    explanation = _hit().model_copy(update={
        "chunk_id": "faq", "citation_role": "interpretation_auxiliary",
        "can_cite_clause": False, "doc_type": "faq", "authority": "public_interpretation",
        "text": "官方解释说明一般规定与例外的关系。", "full_article_text": None,
        "publish_date": "2026-07-24",
    })
    internal = explanation.model_copy(update={"chunk_id": "internal"})
    unregistered = explanation.model_copy(update={"chunk_id": "unknown"})
    uncited_law = _hit().model_copy(update={"chunk_id": "uncited"})
    chunks = {
        hit.chunk_id: Chunk(
            chunk_id=hit.chunk_id, doc_id=hit.doc_id, source_id=hit.source_id,
            title=hit.title, text=hit.text, chunk_index=0, source_url=hit.source_url,
            char_count=len(hit.text), doc_type="faq", citation_role="interpretation_auxiliary",
            library_kind=library, applicable_region="CN", applicable_subjects=["数据处理者"],
        )
        for hit, library in [(explanation, "legal"), (internal, "internal_policy")]
    }
    SemanticGroundingVerifier(model_id="test-model", client=client)(
        review_goal="审查", draft=_draft(), confirmed_intake={}, extracted_facts=ReviewFacts(),
        material="材料", evidence=[_hit(), explanation, internal, unregistered, uncited_law],
        chunks_by_id=chunks,
    )
    payload = json.loads(client.calls[0][1].content)
    assert [a["chunk_id"] for a in payload["cited_authorities"]] == ["c1"]
    assert [a["chunk_id"] for a in payload["context_authorities"]] == ["faq", "uncited"]
    context = payload["context_authorities"][0]
    assert context["can_cite_clause"] is False
    assert context["doc_type"] == "faq"
    assert context["article_text"] == explanation.text
    assert context["publish_date"] == "2026-07-24"
    assert context["applicable_region"] == "CN"
    assert context["applicable_subjects"] == ["数据处理者"]
    assert payload["report_background"]["claims"][0]["supporting_chunk_ids"] == ["c1"]
    assert len(client.calls) == 1


@pytest.mark.parametrize("role", [
    "conditional_local_basis", "conditional_industry_basis", "implementation_reference",
])
def test_returned_context_reaches_full_report_without_clause_authority(role):
    from law_agent.review.result_builder import validate_grounded_claims

    client = FakeClient(outputs=[{
        "status": "supported", "conclusion_reason": "核对正式法条与补充资料",
        "claim_checks": [{"claim_index": 0, "status": "supported", "reason": "支持"}],
    }])
    reference = _hit().model_copy(update={
        "chunk_id": "reference", "source_id": "local_list", "citation_role": role,
        "can_cite_clause": False, "doc_type": "guideline",
        "text": "适用范围、行业清单与办理要求", "full_article_text": None,
    })
    chunk = Chunk(
        chunk_id=reference.chunk_id, doc_id=reference.doc_id, source_id=reference.source_id,
        title=reference.title, text=reference.text, chunk_index=0, source_url=reference.source_url,
        char_count=len(reference.text), citation_role=role, doc_type="guideline",
        can_cite_clause=False, applicable_region="CN-SH", applicable_subjects=["再保险"],
    )
    SemanticGroundingVerifier(model_id="test", client=client)(
        review_goal="核对地方清单适用范围", draft=_draft(), confirmed_intake={},
        extracted_facts=ReviewFacts(), material="材料", evidence=[_hit(), reference],
        chunks_by_id={reference.chunk_id: chunk},
    )
    payload = json.loads(client.calls[0][1].content)
    assert [a["chunk_id"] for a in payload["cited_authorities"]] == ["c1"]
    context = payload["context_authorities"][0]
    assert context["chunk_id"] == "reference" and context["citation_role"] == role
    assert context["can_cite_clause"] is False and context["applicable_region"] == "CN-SH"
    assert context["applicable_subjects"] == ["再保险"]
    assert len(client.calls) == 1
    with pytest.raises(ValueError, match="法条|引用"):
        validate_grounded_claims([
            GroundedClaim(text="一项正式法律义务", supporting_chunk_ids=[reference.chunk_id]),
        ], [reference])


def test_semantic_rejection_returns_to_agent_then_budget_abstains() -> None:
    verdict = SemanticVerdict(
        status="unsupported",
        claim_checks=[{"claim_index": 0, "status": "unsupported", "reason": "数量条件不能排除全部例外"}],
        conclusion_reason="第五条并列免予分支未核查",
    )
    seen = []

    def decide(state, _intake):
        seen.append(state.model_copy(deep=True))
        return AgentDecision(action="finish", summary="提交法律判断", draft=_draft())

    def finalize(_draft, _state):
        raise SemanticGroundingRejected(verdict)

    state = run_agent(
        AgentState(goal="审查数据出境", max_turns=2), material="材料", intake={},
        decide=decide, search=lambda *_, **_kwargs: [], web_search=lambda *_: [], finalize=finalize,
        abstain=lambda draft, _state: {"review_result": draft.model_dump(mode="json")},
        checkpoint=lambda _state: None,
    )

    assert seen[1].steps[0].observation["semantic_grounding"]["status"] == "unsupported"
    assert state.status == "completed"
    assert state.result["review_result"]["risk_level"] == "insufficient_evidence"
    assert state.result["review_result"]["questions"] == []
    assert "并列免予分支" in state.result["review_result"]["conclusion"]


@pytest.mark.parametrize("has_verdict", [False, True])
def test_budget_limit_preserves_typed_gaps_without_turning_feedback_into_fact_tasks(has_verdict):
    legal = "缺新旧规则衔接原文"
    verdict = SemanticVerdict(
        status="uncertain", claim_checks=[], missing_facts=["当前实际人数"],
        core_checks=[ReportCheck(target="conclusion", status="uncertain", reason="法律关系未核清", missing_basis=legal)],
        conclusion_reason="规则与事实仍需核对，这段意见不是申请人应回答的问题。",
    )

    def finalize(draft, state):
        raise SemanticGroundingRejected(verdict)

    state = run_agent(
        AgentState(goal="当前路径", max_turns=1), material="材料", intake={},
        decide=lambda *_: (AgentDecision(action="finish", summary="提交", draft=_draft()) if has_verdict
                           else AgentDecision(action="propose_plan", summary="调查", plan=["核对依据"])),
        search=lambda *_, **_kwargs: [], web_search=lambda *_: [], finalize=finalize,
        abstain=lambda draft, _: {"review_result": draft.model_dump(mode="json")}, checkpoint=lambda _: None,
    )
    result = state.result["review_result"]
    assert [q["text"] for q in result["questions"]] == (["当前实际人数"] if has_verdict else [])
    assert result["legal_basis_gaps"] == ([legal] if has_verdict else [])
    assert result["risk_level"] == "insufficient_evidence" and result["legal_path"] is None
    assert state.steps[-1].observation["unresolved_review_feedback"] in result["conclusion"]


def _core_supported() -> dict:
    return {
        "status": "supported",
        "claim_checks": [{"claim_index": 0, "status": "supported", "reason": "核心有依据"}],
        "conclusion_reason": "核心有依据",
    }


def test_legal_relation_feedback_does_not_become_a_business_fact_request():
    reason = "现有原文未说明两个版本的条件如何衔接，不能先假定旧条件仍适用。"
    client = FakeClient(outputs=[{
        "status": "uncertain", "claim_checks": [{"claim_index": 0, "status": "supported", "reason": "引文准确"}],
        "core_checks": [{"target": "conclusion", "status": "uncertain", "reason": reason,
                         "missing_basis": "新旧条件的修订或过渡依据", "missing_facts": []}],
        "conclusion_reason": reason,
    }])
    draft = _draft().model_copy(update={"legal_basis_gaps": ["新旧条件的修订或过渡依据"]})
    verdict = SemanticGroundingVerifier(model_id="test", client=client)(
        review_goal="当前路径", draft=draft, confirmed_intake={}, extracted_facts=ReviewFacts(),
        material="材料", evidence=[_hit()],
    )
    payload = json.loads(client.calls[0][1].content)
    assert "legal_basis_gaps:0" in payload["core_targets"]
    assert payload["report_background"]["legal_basis_gaps"] == draft.legal_basis_gaps
    assert verdict.status == "uncertain" and verdict.missing_facts == []
    assert revision_targets(draft, verdict, evidence_ids={"c1"})[0]["missing_basis"] == "新旧条件的修订或过渡依据"
    assert len(client.calls) == 1


def test_business_and_legal_gaps_are_core_objects_without_extra_column_calls():
    from law_agent.review.report_grounding import core_report_units

    extra = "全年预计累计量是否会达到另一档门槛"
    legal = "缺最新规则与旧条件的适用衔接依据"
    draft = _draft().model_copy(update={
        "risk_level": "insufficient_evidence", "legal_path": None,
        "questions": [ReviewQuestionDraft(text='当前实际累计人数'), ReviewQuestionDraft(text=extra)], "legal_basis_gaps": [legal],
        "recommended_actions": [],
    })
    response = _core_supported()
    response.update(status="unsupported", core_checks=[{
        "target": "questions:1:text", "status": "unsupported",
        "reason": "原文按当前累计量判断，未来预测不是当前阻塞事实。",
    }])
    client = FakeClient(outputs=[response])
    verdict = SemanticGroundingVerifier(model_id="test", client=client)(
        review_goal="当前路径", draft=draft, confirmed_intake={}, extracted_facts=ReviewFacts(),
        material="材料", evidence=[_hit()],
    )
    core = json.loads(client.calls[0][1].content)
    assert {"questions:0:text", "questions:1:text", "legal_basis_gaps:0"} <= set(core["core_targets"])
    assert core_report_units(draft)["legal_basis_gaps:0"] == legal
    assert verdict.status == "unsupported"
    target = revision_targets(draft, verdict, evidence_ids={"c1"})[0]
    assert target["target"] == "questions:1:text" and target["text"] == extra
    assert len(client.calls) == 1


def test_report_missing_facts_propagate_without_turning_unknown_into_negative():
    check = ReportCheck(
        target="issues:0:finding", status="uncertain", reason="关键事实仍未知",
        quote="签订情况未知", missing_facts=["签订情况"],
    )
    verdict = SemanticVerdict(
        status="supported", claim_checks=[], report_checks=[check], conclusion_reason="核心支持",
    )
    assert verdict.status == "uncertain"
    assert verdict.missing_facts == ["签订情况"]
    assert "issues:0:finding" in verdict.conclusion_reason
