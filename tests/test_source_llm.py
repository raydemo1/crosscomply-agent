from datetime import date

from law_agent.kb.source_inference import ORDER_RE, clean_title
from law_agent.kb.source_llm import read_with_model

TEXT = "中国人民银行令〔2025〕第3号\n《办法》现予发布，自2025年6月30日起施行。\n行长 潘功胜\n2025年5月1日\n第一条 为规范……"


def _field(value, quote=None):
    return {"value": value, "quote": quote}


def _answer(**overrides):
    answer = {
        "title": _field("中国人民银行业务领域数据安全管理办法", "中国人民银行业务领域数据安全管理办法"),
        "doc_type": _field("regulation", "中国人民银行令〔2025〕第3号"),
        "authority": _field("departmental_rule", "中国人民银行令〔2025〕第3号"),
        "citation_role": _field("conditional_industry_basis", "中国人民银行令〔2025〕第3号"),
        "applicable_region": _field("CN"),
        "issuing_body": _field("中国人民银行", "中国人民银行令〔2025〕第3号"),
        "publish_date": _field("2025-05-01", "2025年5月1日"),
        "effective_date": _field("2025-06-30", "自2025年6月30日起施行"),
    }
    return answer | overrides


class _Client:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def chat_json(self, messages):
        self.calls.append(messages)
        return self.answers.pop(0)


def _read(client, text=TEXT, url="https://www.pbc.gov.cn/x/index.html"):
    return read_with_model(client=client, source_id="s", title="中国人民银行令〔2025〕第3号（中国人民银行业务领域数据安全管理办法）",
                           text=text, url=url, source_site="www.pbc.gov.cn", file_format="html",
                           today=date(2026, 1, 1))


def test_order_numbers_in_every_common_form_are_recognised():
    for order in ("令第16号", "令（第9号）", "令〔2025〕第3号", "令【第218号】", "第24号令", "令\n\n第15号"):
        assert ORDER_RE.search(order), order


def test_titles_lose_the_order_wrapper_and_the_full_text_mark():
    assert clean_title("中国人民银行令〔2025〕第3号（中国人民银行业务领域数据安全管理办法）") == "中国人民银行业务领域数据安全管理办法"
    assert clean_title("中华人民共和国反电信网络诈骗法（全文）") == "中华人民共和国反电信网络诈骗法"


def test_a_consistent_reading_is_taken_in_one_call():
    client = _Client(_answer())
    reading = _read(client)
    assert len(client.calls) == 1
    assert reading.inference.settled
    assert reading.inference.source.authority == "departmental_rule"


def test_elided_quotes_nationwide_scope_and_compact_url_dates_hold():
    client = _Client(_answer(
        doc_type=_field("regulation", "中国人民银行令……自2025年6月30日起施行"),
        publish_date=_field("2025-05-01", "20250501"),
    ))
    reading = _read(client, url="https://www.pbc.gov.cn/x/20250501/index.html")
    assert reading.rejected == {}


def test_an_out_of_vocabulary_value_is_left_to_the_reviewer_not_retried():
    client = _Client(_answer(authority=_field("中国人民银行", "中国人民银行令〔2025〕第3号")))  # an issuer, not an authority
    reading = _read(client)
    assert len(client.calls) == 1
    assert "authority" in reading.inference.unresolved and reading.inference.source.authority == "unknown"


def test_a_claim_without_an_enacting_order_is_left_to_the_reviewer():
    text = "国家金融监督管理总局关于印发银行保险机构数据安全管理办法的通知\n金规〔2024〕24号\n第一条 为规范……"
    claim = _answer(doc_type=_field("regulation", "金规〔2024〕24号"), authority=_field("departmental_rule", "金规〔2024〕24号"),
                    issuing_body=_field("国家金融监督管理总局", "国家金融监督管理总局关于印发"),
                    publish_date=_field(None), effective_date=_field(None))
    reading = _read(_Client(claim), text=text)
    assert {"authority", "effective_date"} <= set(reading.inference.unresolved)
    assert not reading.inference.settled


def test_what_the_model_says_it_is_unsure_of_goes_to_the_reviewer():
    reading = _read(_Client(_answer() | {"uncertain": ["citation_role"], "note": "不确定是否只约束金融行业"}))
    assert reading.inference.unresolved == ["citation_role"]
    assert reading.inference.evidence["note"] == "不确定是否只约束金融行业"


def test_a_publication_date_from_the_address_or_the_search_result_needs_no_quote():
    client = _Client(_answer(publish_date=_field("2025-05-28", None)))
    reading = _read(client, url="https://www.pbc.gov.cn/x/2025052819185580666/t20250528_1.html")
    assert "publish_date" not in reading.rejected
