import json

from law_agent.review.agent import AgentModel, AgentState
from law_agent.review.agent_context import RETRIEVAL_FIELDS, compact_evidence_context
from law_agent.review.tool_calling import ToolSession
from tests.test_review_agent import _draft, _hit
from tests.test_review_llm import FakeClient, tool_message


def test_context_can_reconstruct_evidence_with_full_text_and_metadata_variations():
    hits = [
        _hit("c1", "s1", text="完整原文").model_copy(update={"full_article_text": "完整原文"}),
        _hit("c2", "s1", text="片段").model_copy(update={
            "full_article_text": "片段与其余完整原文", "effective_date": "2024-01-01",
            "citation_role": "interpretation_auxiliary", "can_cite_clause": False,
        }),
        _hit("c3", "s2", text="没有额外全文"),
    ]
    state = AgentState(goal="审查", evidence=hits)
    original = state.model_dump(mode="json")
    compact = compact_evidence_context(original)

    assert original == state.model_dump(mode="json")
    assert len(compact["evidence"]) == len(hits)
    for original_hit, compact_hit in zip(original["evidence"], compact["evidence"], strict=True):
        restored = {**compact["evidence_sources"][compact_hit["source_id"]], **compact_hit}
        assert restored.get("full_article_text", restored["text"]) == (
            original_hit["full_article_text"] or original_hit["text"]
        )
        assert {k: v for k, v in restored.items() if k not in {"full_article_text", "outline_read_request"}} == {
            k: v for k, v in original_hit.items() if k not in {*RETRIEVAL_FIELDS, "full_article_text"}
        }
    assert "effective_date" not in compact["evidence_sources"]["s1"]
    assert compact["evidence"][1]["can_cite_clause"] is False


def test_rank_updates_do_not_resend_original_text_but_legal_qualifications_do():
    state = AgentState(goal="审查", evidence=[_hit("c1", "s1", text="完整原文")])
    session = ToolSession()
    project = lambda: {"state": compact_evidence_context(state.model_dump(mode="json"))}
    initial = session.context_delta(project())
    assert initial["state"]["evidence"][0]["text"] == "完整原文"
    state.evidence[0] = state.evidence[0].model_copy(update={"score": 100, "rank": 9,
        "matched_query_ids": ["new-query"], "retriever": "keyword"})
    assert session.context_delta(project()) == {}
    state.evidence[0] = state.evidence[0].model_copy(update={"can_cite_clause": False})
    assert session.context_delta(project())["state"]["evidence"][0]["can_cite_clause"] is False
    state.evidence[0] = state.evidence[0].model_copy(update={"text": "更新后的原文", "effective_date": "2025-01-01"})
    update = session.context_delta(project())["state"]
    assert update["evidence"][0]["text"] == "更新后的原文"
    assert update["evidence_sources"]["s1"]["effective_date"] == "2025-01-01"


def test_agent_model_projects_context_without_changing_facts_or_intake():
    client = FakeClient(outputs=[tool_message(("call_1", 'propose_plan', {'summary': '计划', 'plan': ['核对原文']}))])
    state = AgentState(goal="审查", evidence=[_hit("c1", "s1")], report_draft=_draft())
    snapshot = state.model_dump(mode="json", exclude={"tool_session"})
    intake = {"facts": {"processing_purpose": "业务目的"}, "_material_version_ids": ["mv1"]}

    AgentModel(model_id="test", client=client)(state, intake)

    payload = json.loads(client.calls[0][1].content)
    from law_agent.review.dates import business_today
    assert payload["current_date"] == business_today().isoformat()
    assert payload["state"]["evidence_sources"]["s1"]["title"] == state.evidence[0].title
    assert payload["state"]["evidence"][0]["chunk_id"] == "c1"
    assert payload["state"]["report_draft"] == state.report_draft.model_dump(mode="json")
    assert payload["confirmed_intake"] == {"facts": intake["facts"]}
    assert payload["frozen_material_version_ids"] == ["mv1"]
    assert state.model_dump(mode="json", exclude={"tool_session"}) == snapshot
    assert state.tool_session.pending
    assert "tool_session" not in payload["state"]
