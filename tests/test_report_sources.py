from law_agent.review.report_data import build_legal_sources


def test_report_lists_only_articles_supporting_final_claims() -> None:
    case = {"response": {"review_result": {
        "claims": [{"text": "须履行出境告知义务", "supporting_citation_refs": ["法源-01"]}],
        "citations": [
            {"citation_ref": "法源-01", "title": "个人信息保护法", "article_no": "第三十九条", "usage": "legal_basis", "source_url": "https://example.com/law"},
            {"citation_ref": "法源-02", "title": "办事指南", "article_no": "", "usage": "implementation_reference", "source_url": "https://example.com/guide"},
        ],
    }}}

    sources = build_legal_sources(case)

    assert [(item.title, item.article) for item in sources] == [("个人信息保护法", "第三十九条")]
    assert sources[0].application == "须履行出境告知义务"
