import json

from law_agent.review.agent import AgentModel, AgentState
from law_agent.review.agent_context import compact_evidence_context
from tests.test_review_agent import _draft, _hit
from tests.test_review_llm import FakeClient


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
        assert {k: v for k, v in restored.items() if k != "full_article_text"} == {
            k: v for k, v in original_hit.items() if k != "full_article_text"
        }
    assert "effective_date" not in compact["evidence_sources"]["s1"]
    assert compact["evidence"][1]["can_cite_clause"] is False


def test_agent_model_projects_context_without_changing_checkpoint_or_intake():
    client = FakeClient(outputs=[{"action": "propose_plan", "summary": "计划", "plan": ["核对原文"]}])
    state = AgentState(goal="审查", evidence=[_hit("c1", "s1")], report_draft=_draft())
    snapshot = state.model_dump(mode="json")
    intake = {"facts": {"processing_purpose": "业务目的"}, "_material_version_ids": ["mv1"]}

    AgentModel(model_id="test", client=client)(state, intake)

    payload = json.loads(client.calls[0][1].content)
    assert payload["state"]["evidence_sources"]["s1"]["title"] == state.evidence[0].title
    assert payload["state"]["evidence"][0]["chunk_id"] == "c1"
    assert payload["state"]["report_draft"] == state.report_draft.model_dump(mode="json")
    assert payload["confirmed_intake"] == {"facts": intake["facts"]}
    assert payload["frozen_material_version_ids"] == ["mv1"]
    assert state.model_dump(mode="json") == snapshot
