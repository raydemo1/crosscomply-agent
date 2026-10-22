import pytest

from law_agent.review.qa_agent import QAState, source_citations
from law_agent.review.qa_citations import citation_marker, render_qa_citations
from law_agent.review.qa_context import qa_state_context
from tests.test_review_result_builder import _hit


def sources():
    return source_citations(QAState(goal="q", evidence=[_hit("c1"), _hit("c2")]))


@pytest.mark.parametrize("final", [False, True])
def test_unavailable_source_stays_comparable_but_never_a_legal_citation(final):
    original = sources()[0]
    citation = {**original, "available_at_answer_date": False}
    answer, selected = render_qa_citations("版本对照。" + citation["citation_marker"], [citation], final=final)
    assert "非回答时点有效依据，仅供比较" in answer
    assert selected[0]["kind"] == "context" and selected[0]["can_cite_clause"] is False
    assert selected[0]["excerpt"] == original["excerpt"]
    assert citation["kind"] == original["kind"]


def test_source_qualification_tracks_date_changes_without_mutating_acquired_original():
    from datetime import date

    class TimeTools:
        def evidence_as_of(self, evidence, as_of):
            return evidence if as_of >= date(2026, 10, 15) else []

    state = QAState(goal="版本比较", evidence=[_hit("c1")])
    state.facts.as_of_date = date(2026, 10, 8)
    earlier = source_citations(state, tools=TimeTools())[0]
    assert earlier["can_cite_clause"] is False and earlier["available_at_answer_date"] is False
    state.facts.as_of_date = date(2026, 10, 20)
    later = source_citations(state, tools=TimeTools())[0]
    assert later["can_cite_clause"] is True and later["available_at_answer_date"] is True
    assert state.evidence[0].can_cite_clause is True
    assert earlier["citation_marker"] == later["citation_marker"]


def test_markers_resolve_to_tool_metadata_and_program_owned_numbers():
    available = sources()
    first, second = (c["citation_marker"] for c in available)
    text = f"条件判断。{second}{first}{second} 后文不变。"
    answer, cited = render_qa_citations(text, available)
    assert answer == "条件判断。[依据2][依据1][依据2] 后文不变。"
    assert [c["ref"] for c in cited] == ["依据1", "依据2"]
    assert all("citation_marker" not in c for c in cited)
    assert [c["excerpt"] for c in cited] == [c["excerpt"] for c in available]
    assert first == citation_marker("chunk:c1") and first != second


def test_partial_markers_are_buffered_at_every_stream_boundary():
    available = sources()
    marker = available[1]["citation_marker"]
    for i in range(1, len(marker)):
        answer, cited = render_qa_citations("前文" + marker[:i], available, final=False)
        assert answer == "前文"
        assert cited == []
    assert render_qa_citations("前文" + marker, available)[0] == "前文[依据2]"


@pytest.mark.parametrize("template", ["[ [cite:{identity}] ]", "[[ cite : {identity} ]]", "[\n[cite:{identity}]\n]"])
def test_whitespace_in_marker_delimiters_preserves_exact_source_identity(template):
    available = sources()
    identity = available[1]["citation_marker"][7:-2]
    marker = template.format(identity=identity)
    for end in range(1, len(marker)):
        assert render_qa_citations("判断。" + marker[:end], available, final=False) == ("判断。", [])
    answer, used = render_qa_citations("判断。" + marker, available)
    assert answer == "判断。[依据2]"
    assert [c["chunk_id"] for c in used] == ["c2"]


@pytest.mark.parametrize("text", ["[ [cite:unknown] ]", "[cite:unknown]", "[[ cite : unfinished", "[ [cite:bad]", "[[cite:missing] ]"])
def test_malformed_and_unknown_markers_never_leak_into_completed_or_streamed_answers(text):
    assert render_qa_citations("判断。" + text, sources(), final=False) == ("判断。", [])
    with pytest.raises(ValueError, match="引用"):
        render_qa_citations("判断。" + text, sources())


def test_marker_identity_is_not_fuzzily_repaired_and_markdown_links_remain_intact():
    available = sources()
    marker = available[0]["citation_marker"]
    assert render_qa_citations(marker[:10] + " " + marker[10:], available)[0] == "[依据1]"
    with pytest.raises(ValueError, match="未取得"):
        render_qa_citations(marker[:10] + "f" + marker[11:], available)
    text = "[法律原文](https://example.test/law) 和 [附录]，最后一个["
    assert render_qa_citations(text, available) == (text, [])


def test_buffered_invalid_marker_does_not_publish_sources_from_hidden_text():
    available = sources()
    first, second = (c["citation_marker"] for c in available)
    answer, used = render_qa_citations(f"判断。{first}[cite:broken]隐藏内容{second}", available, final=False)
    assert answer == "判断。[依据1]"
    assert [c["ref"] for c in used] == ["依据1"]


@pytest.mark.parametrize("text", ["[[cite:unknown]]", "[[cite:unknown?]]", "[[cite:unfinished", "[依据1]", "[依据25?]"])
def test_forged_incomplete_and_model_numbered_references_are_not_accepted(text):
    with pytest.raises(ValueError, match="引用"):
        render_qa_citations("结论" + text, sources())


def test_model_context_keeps_original_qualification_without_generated_usage_instructions():
    reference = _hit("c1").model_copy(update={"can_cite_clause": False, "citation_role": "implementation_reference"})
    state = QAState(goal="q", evidence=[reference])
    source_citations(state)
    context = qa_state_context(state.model_dump(mode="json"))
    hit = context["evidence"][0]
    assert hit["citation_marker"] in hit["citation_label"]
    assert "citation_usage" not in hit
    assert hit["citation_role"] == reference.citation_role
    assert hit["text"] == reference.text
    assert hit["can_cite_clause"] is False
    assert "ref" not in hit and "citation_markers" not in context


def test_marker_from_another_run_requires_the_same_source_to_be_acquired_again():
    first = sources()[0]["citation_marker"]
    with pytest.raises(ValueError, match="未取得"):
        render_qa_citations(first, [])


def test_stream_does_not_turn_a_bad_marker_into_a_link_or_retryable_transport_error():
    displayed, used = render_qa_citations("结论[[cite:forged]]", sources(), final=False)
    assert displayed == "结论" and used == []
    with pytest.raises(ValueError, match="未取得"):
        render_qa_citations("结论[[cite:forged]]", sources())
