"""Paired live evaluation must count tool calls and expose no unreturned evidence."""

import pytest

from law_agent.review.agent import EvidenceRead, EvidenceReadRequest
from law_agent.review.schemas import ReviewFacts
from scripts.analyze_navigation import offline_tools
from scripts.compare_navigation_live import FlatRequest, ReadingSession
from tests.test_review_agent import _chunk


@pytest.mark.parametrize("arm", ["flat", "tree"])
def test_directory_is_not_returned_target_and_duplicate_costs_no_budget(arm):
    tools = offline_tools([_chunk("first", "第一条", "正文", index=0)])
    session = ReadingSession(tools, "s1", ReviewFacts(as_of_date="2026-10-19"), arm)
    request = EvidenceReadRequest(source_id="s1") if arm == "tree" else FlatRequest(source_id="s1")
    directory = session.invoke(request)
    assert directory["response"]["hits"] == []
    assert session.evidence == {}
    assert session.invoke(request)["executed"] is False
    assert session.reads == 1
    body = session.invoke(type(request)(source_id="s1", article_no="第一条"))
    assert body["response"]["hits"][0]["chunk_id"] == "first"
    assert set(session.evidence) == {"first"}


def test_failure_consumes_budget_and_eleventh_actual_call_is_rejected():
    class Tools:
        calls = 0

        def read_evidence(self, *args, **kwargs):
            self.calls += 1
            if self.calls < 10:
                raise ValueError("invalid selector")
            return EvidenceRead(hits=[])

    tools = Tools()
    session = ReadingSession(tools, "s1", ReviewFacts(), "tree")
    for index in range(10):
        result = session.invoke(EvidenceReadRequest(source_id="s1", node_id=str(index)))
        assert result["executed"] is True
    assert session.invoke(EvidenceReadRequest(source_id="s1", node_id="extra"))["executed"] is False
    assert tools.calls == session.reads == 10
    assert session.evidence == {}


def test_cross_source_precheck_does_not_call_tool():
    session = ReadingSession(None, "s1", ReviewFacts(), "tree")
    assert session.invoke(EvidenceReadRequest(source_id="s2"))["executed"] is False
    assert session.reads == 0
