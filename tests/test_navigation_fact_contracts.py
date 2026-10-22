"""Original-source navigation and consistent fact-use contracts."""

from copy import deepcopy

import pytest

from law_agent.review.agent import (
    AgentState,
    AgentStep,
    EvidenceReadRequest,
    _read_evidence_request,
    review_context,
)
from law_agent.review.agent_tools import ComplianceAgentTools
from law_agent.review.qa_agent import QAState
from law_agent.review.qa_context import qa_state_context
from law_agent.review.retrieval.neighbors import hit_from_chunk
from tests.test_review_agent import _tools_with
from tests.test_source_navigation import chunk


@pytest.mark.parametrize("qa", [False, True])
def test_source_directory_entry_is_directly_readable_in_review_and_qa(qa):
    chunks = [chunk(0, ["适用范围"], can_cite_clause=False), chunk(1, ["业务表格"], can_cite_clause=False)]
    tools = _tools_with(chunks)
    state_type = QAState if qa else AgentState
    state = state_type(goal="查原文", evidence=[hit_from_chunk(chunks[0], 0)])
    before = deepcopy(state.model_dump(mode="json"))
    context = qa_state_context(deepcopy(before)) if qa else review_context(state, {})["state"]
    source = context["evidence_sources"]["s1"]
    request = EvidenceReadRequest.model_validate(source["outline_read_request"])
    assert request.source_id == "s1" and request.node_id is None
    assert state.model_dump(mode="json") == before
    result = _read_evidence_request(state, request, read_evidence=tools.read_evidence, checkpoint=lambda _: None)
    assert result["read_mode"] == "outline" and result["chunk_ids"] == []
    assert [entry["title"] for entry in result["outline"]] == ["适用范围", "业务表格"]
    assert len(state.evidence) == 1 and state.evidence[0].can_cite_clause is False
    text = _read_evidence_request(state, EvidenceReadRequest(source_id="s1", node_id=result["outline"][1]["node_id"]),
                                  read_evidence=tools.read_evidence, checkpoint=lambda _: None)
    assert text["chunk_ids"] == ["c1"] and state.evidence[-1].can_cite_clause is False


def test_unread_keyword_source_offers_the_same_directory_entry_without_citing_preview():
    navigation = ComplianceAgentTools._group_search_navigation([{
        "source_id": "s1", "title": "文件", "source_has_articles": False,
        "chunk_id": "c0", "article_no": None, "preview": "仅导航预览",
    }])
    state = AgentState(goal="查来源", steps=[AgentStep(number=1, action="search_evidence", summary="发现来源",
        observation={"keyword_candidates": navigation})])
    tools = _tools_with([chunk(0, ["原文目录"], can_cite_clause=False)])
    request = EvidenceReadRequest.model_validate(navigation[0]["outline_read_request"])
    result = _read_evidence_request(state, request, read_evidence=tools.read_evidence, checkpoint=lambda _: None)
    assert result["read_mode"] == "outline"
    assert state.evidence == []
    assert set(navigation[0]) == {"source_id", "title", "source_has_articles", "outline_read_request", "locations"}
