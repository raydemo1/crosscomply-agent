"""Later provisions that name an older document are found in the corpus and shown with that document."""

from datetime import date

from law_agent.review.agent_context import compact_evidence_context
from law_agent.review.qa_agent import QAState
from law_agent.review.retrieval.later_provisions import index_later_provisions
from law_agent.review.retrieval.neighbors import hit_from_chunk
from law_agent.review.schemas import ReviewFacts
from tests.test_qa_retrieval import query
from tests.test_review_agent import _chunk, _search_tools, _tools_with

OLD_TEXT = "第四条 自上年1月1日起累计向境外提供个人信息不满10万人的，可以订立标准合同。" * 2


def _corpus():
    old = _chunk("old4", "第四条", OLD_TEXT, index=0).model_copy(update={
        "source_id": "scc_2023", "title": "个人信息出境标准合同办法", "publish_date": "2023-02-22",
        "effective_date": "2023-06-01"})
    rule = _chunk("new13", "第十三条", "第十三条 《个人信息出境标准合同办法》等相关规定与本规定不一致的，适用本规定。",
                  index=0).model_copy(update={"source_id": "flow_2024", "title": "促进和规范数据跨境流动规定",
                                              "publish_date": "2024-03-22", "effective_date": "2024-03-22"})
    basis = _chunk("new1", "第一条", "第一条 根据《个人信息出境标准合同办法》，制定本规定。",
                   index=1).model_copy(update={"source_id": "flow_2024", "title": "促进和规范数据跨境流动规定",
                                               "publish_date": "2024-03-22", "effective_date": "2024-03-22"})
    return [old, rule, basis]


def test_only_precedence_language_in_a_later_document_links_back():
    # The basis clause names the old title too, but says nothing about which rule prevails.
    assert index_later_provisions(_corpus()) == {"scc_2023": ["new13"]}


def test_reading_an_old_article_shows_the_later_provision_in_force_at_the_review_date():
    tools = _tools_with(_corpus())
    read = tools.read_evidence("scc_2023", "第四条", None, ReviewFacts(as_of_date=date(2026, 10, 15)))
    assert [(p["source_id"], p["article_no"]) for p in read.hits[0].later_provisions] == [("flow_2024", "第十三条")]
    context = compact_evidence_context(QAState(goal="q", evidence=read.hits).model_dump(mode="json"))
    assert context["evidence_sources"]["scc_2023"]["later_provisions"][0]["text"].endswith("适用本规定。")
    # Before the later rule was published, nothing from the future is shown.
    earlier = tools.read_evidence("scc_2023", "第四条", None, ReviewFacts(as_of_date=date(2023, 10, 1)))
    assert earlier.hits[0].later_provisions == []


def test_search_results_carry_later_provisions_and_name_the_document_type():
    chunks = _corpus()
    filler = _chunk("filler", "第二条", "其他规则" * 40, index=1).model_copy(update={"doc_type": "faq",
                                                                                 "can_cite_clause": False})
    ranked = [hit_from_chunk(c, i).model_copy(update={"score": 10 - i}) for i, c in enumerate([chunks[0], filler])]
    tools = _search_tools([*chunks, filler], lambda *_a, **_k: [ranked], top_k=1)
    result = tools.search([query()], ReviewFacts(as_of_date=date(2026, 10, 15)))
    assert [h.chunk_id for h in result] == ["old4"] and result[0].later_provisions
    assert [(source["doc_type"], [loc.get("acquired", False) for loc in source["locations"]])
            for source in tools.search_results()] == [("law", [True]), ("faq", [False])]
