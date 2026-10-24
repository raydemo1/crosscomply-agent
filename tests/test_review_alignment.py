import json

import pytest

from law_agent.review.agent import AgentDecision, AgentModel, AgentState, run_agent
from law_agent.review.result_builder import (
    ReviewQuestionDraft,
    WebImpactDraft,
    build_result_generation_messages,
    build_review_result_with_deepseek,
    inject_citation_markers,
    report_evidence,
)
from law_agent.review.retrieval.neighbors import hit_from_chunk
from law_agent.review.schemas import EvidenceSelfCheck, GroundedClaim, ReviewFacts
from law_agent.review.semantic_grounding import SemanticGroundingVerifier, SemanticVerdict
from tests.test_review_agent import _chunk, _draft, _tools_with
from tests.test_review_llm import FakeClient, tool_message


def test_review_and_qa_share_investigation_tools_with_independent_delivery():
    from law_agent.review.agent import INVESTIGATION_TOOLS
    from tests.test_qa_native_answers import run_qa

    review = AgentModel(model_id="test", client=FakeClient())
    review_tools = {t["function"]["name"]: t["function"] for t in review.protocol.tools}
    qa_client = FakeClient(outputs=[{"content": "当前资料不足以确定，需结合实际规则判断。"}])
    run_qa(qa_client)
    qa_tools = {t["function"]["name"]: t["function"] for t in qa_client.kwargs[0]["tools"]}
    for name, shared in INVESTIGATION_TOOLS.items():
        assert review_tools[name]["description"] == shared.description
        assert qa_tools[name]["description"].startswith(shared.description)
        if name != "search_evidence":
            assert qa_tools[name]["description"] == shared.description
            for field in shared.fields:
                assert (qa_tools[name]["parameters"]["properties"][field]
                        == review_tools[name]["parameters"]["properties"][field])
            assert qa_tools[name]["parameters"].get("$defs") == review_tools[name]["parameters"].get("$defs")
    assert "research_context" in qa_tools["search_evidence"]["parameters"]["properties"]
    assert "research_context" not in review_tools["search_evidence"]["parameters"]["properties"]
    assert "draft" in review_tools["finish"]["parameters"]["properties"]
    assert "draft" not in qa_tools["finish"]["parameters"]["properties"]


def test_review_first_decision_gets_original_material_window_without_freezing_inferences():
    material = "【材料 mv1】\n" + "原始业务陈述" * 2200

    def decide(state, intake):
        preview = state.model_dump()["material_preview"]
        assert preview["text"] == material[:12000]
        assert preview["next_read_request"] == {"offset": 12000}
        assert state.fact_ledger == []
        assert state.facts == ReviewFacts()
        return AgentDecision(action="finish", summary="提交", draft=_draft())

    state = run_agent(AgentState(goal="审查"), material=material, intake={}, decide=decide,
                      search=lambda *_: [], web_search=lambda *_: [],
                      finalize=lambda *_: {"ok": True}, checkpoint=lambda _: None)
    assert state.turns == 1 and state.reads == 0


def test_report_generation_receives_full_original_text_and_source_qualification():
    text = "一般要求。" * 300 + "但是本规则不适用于例外对象。"
    material = "业务记录。" * 650 + "最后一项业务条件已明确。"
    hit = hit_from_chunk(_chunk("law", "第一条", text, index=0), 0)
    messages = build_result_generation_messages(
        facts=ReviewFacts(), self_check=EvidenceSelfCheck(status="sufficient"),
        evidence_hits=[hit], material_text=material,
    )
    payload = json.loads(messages[1].content)
    assert payload["material_excerpt"] == material
    passage = payload["evidence"][0]
    restored = {**payload["evidence_sources"][passage["source_id"]], **passage}
    assert restored["text"] == text
    assert restored["can_cite_clause"] is True
    assert restored["law_status"] == "effective"
    assert "json_example" not in payload
    assert "完整业务待确认清单写questions" in messages[0].content


def test_verifier_receives_complete_report_and_both_target_groups():
    draft = _draft().model_copy(update={"claims": [], "risk_boundaries": ["本节仅适用于满足给定条件的分支。"]})
    client = FakeClient(outputs=[{"status": "supported", "claim_checks": [], "conclusion_reason": "有依据"}])
    SemanticGroundingVerifier(model_id="test", client=client)(
        review_goal="审查", draft=draft, confirmed_intake={}, extracted_facts=ReviewFacts(),
        material="原文", evidence=[],
    )
    payload = json.loads(client.calls[0][1].content)
    assert payload["report_background"] == draft.model_dump(mode="json")
    assert "draft" not in payload
    assert "risk_boundaries:0" not in payload["core_targets"]
    assert "risk_boundaries:0" in payload["report_targets"]
    assert len(client.calls) == 1


@pytest.mark.parametrize("include_reference", [False, True])
def test_report_only_displays_used_sources_but_verifier_and_audit_keep_all_acquired_evidence(include_reference):
    chunks = [_chunk(cid, article, text, index=i) for i, (cid, article, text) in enumerate([
        ("used", "第一条", "第一条 已引用的一般规则。"),
        ("unused", "第二条", "第二条 尚未在报告中使用的相反规则。"),
    ])]
    chunks.append(_chunk("reference", "", "标准建议的实施措施。", index=2).model_copy(update={
        "doc_type": "guideline", "citation_role": "implementation_reference", "can_cite_clause": False,
    }))
    tools = _tools_with(chunks)
    tools._neighbor_hits, tools._material_versions_by_id = {}, {}
    tools._top_k, tools._material_text = 10, "材料"
    received = []

    def verify(**kwargs):
        received.extend(kwargs["evidence"])
        return SemanticVerdict(status="supported", claim_checks=[], conclusion_reason="测试协议")

    tools._semantic_verifier = verify
    draft = _draft().model_copy(update={
        "claims": [GroundedClaim(text="已引用的一般规则。", supporting_chunk_ids=["used"])],
        "reference_chunk_ids": ["reference"] if include_reference else [],
    })
    state = AgentState(goal="审查", evidence=[hit_from_chunk(c, i) for i, c in enumerate(chunks)])
    result = tools.finalize(draft, state, case_id="case", intake_snapshot={"facts": {}})
    assert [c["chunk_id"] for c in result["review_result"]["citations"]] == (
        ["used", "reference"] if include_reference else ["used"]
    )
    if include_reference:
        assert result["review_result"]["citations"][-1]["can_cite_clause"] is False
    assert {h.chunk_id for h in received} == {"used", "unused", "reference"}
    assert {h["chunk_id"] for h in result["evidence_chunks"]} == {"used", "unused", "reference"}


def test_report_does_not_guess_a_citation_position_from_shared_vocabulary():
    report = "应核对访问日志保存范围、期限和处理目的。"
    claim = GroundedClaim(text="无需保存访问日志，处理目的不能要求保存期限。", supporting_chunk_ids=["c1"])
    assert inject_citation_markers(report, [claim], citation_ref_by_chunk_id={"c1": "法源-01"}) == report


def test_reference_selection_keeps_original_qualification_and_rejects_unacquired_ids():
    from law_agent.review.result_builder import validate_grounded_claims

    hit = hit_from_chunk(_chunk("reference", "", "参考措施", index=0), 0).model_copy(update={
        "can_cite_clause": False, "citation_role": "implementation_reference", "doc_type": "guideline",
    })
    assert report_evidence([hit], claims=[], reference_chunk_ids=["reference"]) == [hit]
    with pytest.raises(ValueError, match="未取得"):
        report_evidence([hit], claims=[], reference_chunk_ids=["other-run"])
    with pytest.raises(ValueError, match="不可作为正式法条"):
        validate_grounded_claims([GroundedClaim(text="法定义务", supporting_chunk_ids=["reference"])], [hit])


def test_report_paragraph_citations_keep_all_supporting_sources_and_do_not_guess_ambiguous_locations():
    text = "处理目的与方法已经明确，但适用条件尚未确认。"
    claim = GroundedClaim(text=text, supporting_chunk_ids=["a", "b"])
    refs = {"a": "法源-01", "b": "法源-02"}
    result = inject_citation_markers(text, [claim], refs)
    assert result.startswith(text)
    assert result.count("<sup") == 2
    assert all(f'data-citation-ref="{ref}"' in result for ref in refs.values())
    repeated = text + "\n\n" + text
    assert inject_citation_markers(repeated, [claim], refs) == repeated


def test_review_navigation_deduplicates_model_input_but_keeps_raw_audit_and_native_thought():
    source = {"source_id": "s", "title": "待查来源", "locations": [{"chunk_id": "unread", "preview": "待查原文"}]}
    outputs = [tool_message((f"c{i}", "search_evidence", {
        "summary": "检索", "queries": [{"query_id": "q", "text": "适用条件", "query_type": "legal_issue"}],
    }), reasoning=f"provider-thought-{i}") for i in range(2)]
    outputs.append(tool_message(("done", "finish", {"summary": "提交", "draft": _draft().model_dump(mode="json")})))
    client = FakeClient(outputs=outputs)
    model = AgentModel(model_id="test", client=client)
    state = run_agent(AgentState(goal="审查"), material="原始材料", intake={}, decide=model,
                      search=lambda *_: [], search_navigation=lambda: [source],
                      web_search=lambda *_: [], finalize=lambda *_: {"ok": True}, checkpoint=lambda _: None)
    observations = [json.loads(next(m.content for m in reversed(call) if m.role == "tool"))["observation"] for call in client.calls[1:]]
    assert observations[0]["keyword_candidates"] == [source]
    assert observations[1]["keyword_candidates"] == []
    assert all(step.observation["keyword_candidates"] == [source] for step in state.steps[:2])
    assert [m.reasoning_content for m in client.calls[-1] if m.role == "assistant"] == [
        "provider-thought-0", "provider-thought-1",
    ]


def test_selected_legal_path_is_preserved_in_structured_report():
    draft = _draft().model_copy(update={
        "risk_level": "medium", "legal_path": "已由事实和法源支持的路径",
        "decision_summary": "根据已提供的材料与适用原文，可以支持本次限定范围内的合规路径判断；本次结果不对范围外事项作出认定。",
        "conclusion": "材料支持这一限定判断。", "questions": [],
        "claims": [GroundedClaim(text="材料支持这一限定判断。", supporting_chunk_ids=["c1"])],
    }).model_dump(mode="json")
    result = build_review_result_with_deepseek(
        review_result_id="r", review_case_id="c", trace_id="t", facts=ReviewFacts(),
        self_check=EvidenceSelfCheck(status="sufficient"),
        evidence_hits=[hit_from_chunk(_chunk("c1", "第一条", "第一条 测试规则。", index=0), 0)],
        client=FakeClient(outputs=[draft]), max_retries=0,
    )
    assert result.legal_path == draft["legal_path"]


def test_structured_report_cannot_deliver_a_determined_path_with_insufficient_evidence():
    from law_agent.review.llm import ReviewWorkflowFailed

    draft = _draft().model_copy(update={"legal_path": "尚未确定的分支"}).model_dump(mode="json")
    with pytest.raises(ReviewWorkflowFailed):
        build_review_result_with_deepseek(
            review_result_id="r", review_case_id="c", trace_id="t", facts=ReviewFacts(),
            self_check=EvidenceSelfCheck(status="insufficient"), evidence_hits=[],
            client=FakeClient(outputs=[draft]), max_retries=0,
        )


def test_legal_basis_gaps_survive_report_generation_and_do_not_become_facts():
    draft = _draft().model_dump(mode="json")
    business_gaps = [q["text"] for q in draft["questions"]]
    gap = "待取得规则修订后的适用范围原文"
    draft["legal_basis_gaps"] = [gap]
    result = build_review_result_with_deepseek(
        review_result_id="r", review_case_id="c", trace_id="t", facts=ReviewFacts(),
        self_check=EvidenceSelfCheck(status="insufficient"), evidence_hits=[],
        client=FakeClient(outputs=[draft]), max_retries=0,
    )
    assert result.legal_basis_gaps == [gap]
    assert result.missing_information == business_gaps
    assert gap not in result.missing_information


def test_agent_finalizer_preserves_legal_basis_gaps_and_actual_review_date():
    from datetime import date

    from law_agent.review.agent_tools import agent_evidence_self_check

    tools = _tools_with([])
    tools._neighbor_hits = {}
    tools._top_k = 10
    tools._material_text = "原始材料"
    tools._material_versions_by_id = {}
    calls = []

    def verify(**kwargs):
        calls.append(kwargs)
        return SemanticVerdict(status="supported", claim_checks=[], conclusion_reason="忠实说明缺口")

    tools._semantic_verifier = verify
    draft = _draft().model_copy(update={"questions": [], "legal_basis_gaps": ["缺新旧规则适用关系原文"]})
    state = AgentState(goal="历史规则适用", facts=ReviewFacts(as_of_date="2024-04-01"))
    payload = tools.finalize(draft, state, case_id="case", intake_snapshot={"facts": {}})
    assert calls[0]["as_of_date"] == date(2024, 4, 1)
    assert payload["review_result"]["legal_basis_gaps"] == draft.legal_basis_gaps
    assert payload["review_result"]["missing_information"] == []
    assert payload["freshness_hold"] is True
    check = agent_evidence_self_check(draft, SemanticVerdict(status="supported", claim_checks=[], conclusion_reason="缺口真实"))
    assert check.status == "insufficient"
    assert [issue.issue_type for issue in check.issues] == ["no_primary_legal_basis"]


@pytest.mark.parametrize("risk_level,legal_path", [("medium", None), ("insufficient_evidence", "确定路径")])
def test_legal_basis_gap_blocks_determined_results_before_model_verification(risk_level, legal_path):
    tools = _tools_with([])
    draft = _draft().model_copy(update={"risk_level": risk_level, "legal_path": legal_path,
                                        "legal_basis_gaps": ["缺决定当前路径的依据"]})
    with pytest.raises(ValueError, match="法律依据尚有缺口"):
        tools.finalize(draft, AgentState(goal="审查"), case_id="case", intake_snapshot={"facts": {}})


@pytest.mark.parametrize("web_impact,gaps,expected", [
    ("none", [], False), ("supplement", [], False),
    ("core", [], True), ("supplement", ["待取得规则附件全文"], True),
    ("none", ["待核适用关系"], True),
])
def test_finalizer_passes_actual_web_observations_and_derives_source_hold(web_impact, gaps, expected):
    from law_agent.review.agent import AgentStep
    from law_agent.review.web_research import WebFinding
    from law_agent.review.workflow import review_needs_source_verification

    finding = WebFinding(title="官方指引发布", url="https://www.gov.cn/current-guide",
                         published_date="2026-10-01", excerpt="发布页摘录，未取得附件全文。")
    tools = _tools_with([])
    tools._neighbor_hits, tools._material_versions_by_id = {}, {}
    tools._top_k, tools._material_text = 10, "材料"
    received = []

    def verify(**kwargs):
        received.append(kwargs)
        return SemanticVerdict(status="supported", claim_checks=[], conclusion_reason="协议测试")

    tools._semantic_verifier = verify
    state = AgentState(goal="审查", steps=[AgentStep(
        number=1, action="search_web", summary="查现行指引",
        observation={"findings": [finding.model_dump(mode="json")]},
    )])
    draft = _draft().model_copy(update={
        "questions": [], "legal_basis_gaps": gaps,
        "web_impacts": [] if web_impact == "none" else [WebImpactDraft(url=finding.url, impact=web_impact)],
    })
    result = tools.finalize(draft, state, case_id="case", intake_snapshot={"facts": {}})
    assert [f.model_dump(mode="json") for f in received[0]["web_findings"]] == result["web_findings"] == [finding.model_dump(mode="json")]
    assert received[0]["evidence"] == []
    assert result["review_result"]["citations"] == []
    assert result["freshness_hold"] is expected
    assert review_needs_source_verification(result) is expected


def test_report_cannot_submit_a_web_discovery_as_a_formal_clause():
    tools = _tools_with([])
    tools._neighbor_hits, tools._material_versions_by_id = {}, {}
    tools._top_k, tools._material_text = 10, "材料"
    draft = _draft().model_copy(update={
        "claims": [GroundedClaim(text="搜索摘录不能成为法条", supporting_chunk_ids=["https://www.gov.cn/current-guide"])],
    })
    with pytest.raises(ValueError, match="未检索到"):
        tools.finalize(draft, AgentState(goal="审查"), case_id="case", intake_snapshot={"facts": {}})


@pytest.mark.parametrize("questions", [[], ["  "], ["缺指引全文"]])
def test_fact_issues_cannot_silently_bypass_the_canonical_task_list(questions):
    from law_agent.review.result_builder import ReviewIssueDraft

    tools = _tools_with([])
    draft = _draft().model_copy(update={"questions": [ReviewQuestionDraft(text=text) for text in questions], "legal_basis_gaps": ["缺指引全文"], "issues": [ReviewIssueDraft(
        kind="missing_information", title="接收方未知", finding="接收方地区尚待确认。",
        question_indexes=[0], material_evidence=[], supporting_chunk_ids=[],
        recommended_action="确认所在地区。",
    )]})
    with pytest.raises(ValueError, match="questions"):
        tools.finalize(draft, AgentState(goal="审查"), case_id="case", intake_snapshot={"facts": {}})


@pytest.mark.parametrize("known_source_id,refresh_needed,official,eligible", [
    (None, False, True, True),
    ("existing", False, True, False),
    ("existing", True, True, True),
    (None, True, False, False),
])
def test_review_exposes_exact_web_candidates_accepted_by_finish(
    known_source_id, refresh_needed, official, eligible,
):
    from law_agent.review.agent import AgentStep
    from law_agent.review.web_research import WebFinding

    finding = WebFinding(
        title="来源", url="https://www.gov.cn/guide" if official else "https://example.com/guide",
        known_source_id=known_source_id, refresh_needed=refresh_needed,
    )
    state = AgentState(goal="审查", steps=[AgentStep(
        number=1, action="search_web", summary="调查公开来源",
        observation={"findings": [finding.model_dump(mode="json")]},
    )])
    client = FakeClient(outputs=[tool_message(("plan", "propose_plan", {
        "summary": "制定计划", "plan": ["核实适用关系"],
    }))])
    AgentModel(model_id="test", client=client)(state, {})
    payload = json.loads(client.calls[0][1].content)
    assert payload["eligible_material_web_urls"] == ([finding.url] if eligible else [])

    tools = _tools_with([])
    tools._neighbor_hits, tools._material_versions_by_id = {}, {}
    tools._top_k, tools._material_text = 10, "材料"
    received = []

    def verify(**kwargs):
        received.append(kwargs)
        return SemanticVerdict(status="supported", claim_checks=[], conclusion_reason="协议测试")

    tools._semantic_verifier = verify
    draft = _draft().model_copy(update={
        "questions": [], "web_impacts": [WebImpactDraft(url=finding.url, impact="supplement")],
    })
    if eligible:
        tools.finalize(draft, state, case_id="case", intake_snapshot={"facts": {}})
        assert len(received) == 1
    else:
        with pytest.raises(ValueError, match="只能包含新官方材料"):
            tools.finalize(draft, state, case_id="case", intake_snapshot={"facts": {}})
        assert received == []


@pytest.mark.parametrize("impact", ["supplement", "execution_detail", "core"])
def test_web_impact_requires_its_own_url(impact):
    with pytest.raises(ValueError):
        WebImpactDraft.model_validate({"impact": impact})
