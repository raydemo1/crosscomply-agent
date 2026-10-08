"""Matter Q&A retains source boundaries and cannot change formal state."""

import json
from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from law_agent.data.schemas import Chunk
from law_agent.review.http.schemas import FactConfirmRequest, MatterQuestionRequest
from law_agent.review.matter_agent import answer_matter, matter_context
from law_agent.review.semantic_grounding import SemanticGroundingRejected, SemanticVerdict
from tests.test_fact_confirmation import binding, confirm, make_matter
from tests.test_review_llm import FakeClient
from tests.test_review_result_builder import _hit


@pytest.fixture
def matter(tmp_path):
    return make_matter(tmp_path)


def reply(**changes):
    return {"answer": "材料只说明客户信息传输至境外，尚缺统计口径，不能确定具体法律路径。", **changes}


def supported(**_kwargs):
    return SemanticVerdict(status="supported", claim_checks=[], conclusion_reason="忠实暂定")


def test_frozen_context_excludes_other_versions_and_unverified_formal_report(matter):
    m = matter
    context = matter_context(m.cases, m.enterprise, m.case["id"], MatterQuestionRequest(**binding(m), question="缺什么"))
    assert context["materials"][0]["text"] == "客户信息传输至境外"
    assert context["confirmed_intake"] == m.intake.intake and context["formal_report"] is None
    assert context["verified_evidence"] == []
    with pytest.raises(ValueError, match="版本已变化"):
        matter_context(m.cases, m.enterprise, "another-case", MatterQuestionRequest(**binding(m), question="问题"))


def test_answer_quotes_exact_material_and_uses_ledger_without_writes(matter):
    m = matter
    context = matter_context(m.cases, m.enterprise, m.case["id"], MatterQuestionRequest(**binding(m), question="材料在哪里"))
    seen = []
    def verifier(**kwargs):
        seen.append(kwargs)
        return supported()
    client = FakeClient(outputs=[reply(material_citations=[{"material_version_id": context["materials"][0]["id"], "quote": "客户信息传输至境外"}])])
    before = deepcopy((m.cases.cases, m.enterprise.tasks))
    result = answer_matter("材料在哪里", context, client=client, verifier=verifier)
    assert result["material_citations"][0]["filename"] == "facts.txt"
    assert seen[0]["fact_ledger"] and seen[0]["confirmed_intake"] == m.intake.intake
    assert seen[0]["review_goal"] == "材料在哪里"
    assert (m.cases.cases, m.enterprise.tasks) == before


@pytest.mark.parametrize("changes", [
    {"claims": [{"text": "确定走某法律路径", "supporting_chunk_ids": ["invented"]}]},
    {"material_citations": [{"material_version_id": "invented", "quote": "客户信息"}]},
    {"material_citations": [{"material_version_id": "current", "quote": "不存在的原文"}]},
])
def test_forged_citations_never_reach_semantic_verifier(matter, changes):
    m = matter
    context = matter_context(m.cases, m.enterprise, m.case["id"], MatterQuestionRequest(**binding(m), question="为什么"))
    for c in changes.get("material_citations", []):
        if c["material_version_id"] == "current":
            c["material_version_id"] = context["materials"][0]["id"]
    def forbidden(**_kwargs):
        pytest.fail("invalid citation reached verifier")
    with pytest.raises(ValueError):
        answer_matter("为什么", context, client=FakeClient(outputs=[reply(**changes)]), verifier=forbidden)


def test_unsupported_reply_and_oversized_context_rejected(matter):
    m = matter
    context = matter_context(m.cases, m.enterprise, m.case["id"], MatterQuestionRequest(**binding(m), question="为什么"))
    with pytest.raises(SemanticGroundingRejected):
        answer_matter("为什么", context, client=FakeClient(outputs=[reply()]), verifier=lambda **_: SemanticVerdict(status="uncertain", claim_checks=[], conclusion_reason="断言缺乏支持"))
    client = FakeClient(outputs=[reply()])
    context["materials"][0]["text"] = "过长" * 70_000
    with pytest.raises(ValueError, match="容量"):
        answer_matter("为什么", context, client=client, verifier=supported)
    assert not client.calls


def test_human_input_without_source_is_rejected(matter):
    m = matter
    m.task.agent_state["steps"] = [{"action": "human_input", "observation": {"answer": "我说人数为零"}}]
    with pytest.raises(ValueError, match="缺少有效来源"):
        matter_context(m.cases, m.enterprise, m.case["id"], MatterQuestionRequest(**binding(m), question="人数"))


def test_conversation_only_writes_event_and_rejects_inflight_version_change(matter):
    m = matter
    m.app.state.matter_answerer = lambda *_: {**reply(), "claims": [], "material_citations": [], "fact_fields": [], "proposed_facts": {}}
    with TestClient(m.app) as client:
        client.post("/api/auth/login", json={"username": m.user.username, "password": "pw"})
        before = deepcopy((m.cases.cases, m.enterprise.tasks))
        response = client.post(f"/api/cases/{m.case['id']}/conversation", json={**binding(m), "question": "还缺什么"})
        assert response.status_code == 200, response.text
        assert (m.cases.cases, m.enterprise.tasks) == before
        assert len(client.get(f"/api/cases/{m.case['id']}/conversation").json()["items"]) == 1
        def change(*_args):
            m.enterprise.create_intake_snapshot(case_id=m.case["id"], material_snapshot_id=m.material.id, intake=IntakePayload(destination_region="日本").model_dump(mode="json"), created_by=m.user.id)
            return response.json()["reply"]
        from law_agent.review.http.schemas import IntakePayload
        m.app.state.matter_answerer = change
        stale = client.post(f"/api/cases/{m.case['id']}/conversation", json={**binding(m), "question": "继续"})
        assert stale.status_code == 409
        assert len(client.get(f"/api/cases/{m.case['id']}/conversation").json()["items"]) == 1


def test_actual_fact_proposal_requires_explicit_owner_confirmation(matter):
    m = matter
    m.task.status = "succeeded"
    event = m.cases.add_event(m.case["id"], m.user.id, event_type="matter_agent_turn", payload={
        **binding(m), "input_provenance": "applicant_statement", "question": "实际接收地是日本", "reply": {"proposed_facts": {"destination_region": "日本"}},
    })
    assert m.cases.get_case(m.case["id"])["intake"]["destination_region"] == ""
    request = FactConfirmRequest(**binding(m), conversation_id=event["id"], values={"destination_region": "日本"}, confirmed=True)
    queued = confirm(m, request)
    assert m.enterprise.get_task(queued["task_id"]).intake_snapshot_id != m.intake.id


def test_verified_legal_reply_preserves_applicability_and_checks_current_corpus(matter, monkeypatch):
    from law_agent.review import matter_agent

    m = matter
    context = matter_context(m.cases, m.enterprise, m.case["id"], MatterQuestionRequest(**binding(m), question="地方依据"))
    hit = _hit()
    chunk = Chunk(
        chunk_id=hit.chunk_id, doc_id=hit.doc_id, source_id=hit.source_id, title=hit.title,
        text=hit.text, char_count=len(hit.text), chunk_index=0, source_url=hit.source_url,
        can_cite_clause=hit.can_cite_clause, citation_role=hit.citation_role,
        applicable_region="CN-SH", applicable_subjects=["再保险"],
    )
    monkeypatch.setattr(matter_agent, "load_corpus", lambda *_: [chunk])
    context["verified_evidence"] = [hit.model_dump(mode="json")]
    client = FakeClient(outputs=[
        reply(claims=[{"text": "该规则具有适用范围，仍需确认本案是否落入。", "supporting_chunk_ids": [hit.chunk_id]}]),
        {"status": "supported", "claim_checks": [{"claim_index": 0, "status": "supported", "reason": "仅作条件性解释"}], "conclusion_reason": "保留适用边界"},
    ])
    answer_matter("地方依据", context, client=client)
    check = json.loads(client.calls[1][1].content)["cited_authorities"][0]
    assert check["applicable_region"] == "CN-SH" and check["applicable_subjects"] == ["再保险"]
    chunk.can_cite_clause = False
    empty = FakeClient(outputs=[reply()])
    with pytest.raises(ValueError, match="法源库已变化"):
        answer_matter("地方依据", context, client=empty)
    assert not empty.calls


def test_conversation_facts_are_unverified_and_reviewer_proposals_are_not_facts(matter):
    m = matter
    for provenance in ["applicant_statement", "reviewer_instruction"]:
        m.cases.add_event(m.case["id"], m.user.id, event_type="matter_agent_turn", payload={
            **binding(m), "input_provenance": provenance, "question": "新事实", "reply": {"proposed_facts": {"destination_region": "日本"}},
        })
    context = matter_context(m.cases, m.enterprise, m.case["id"], MatterQuestionRequest(**binding(m), question="核对"))
    statements = [e for e in context["fact_ledger"] if e["source_type"] == "applicant_statement"]
    assert len(statements) == 1 and statements[0]["status"] == "unverified"
    assert context["confirmed_intake"]["destination_region"] == ""
