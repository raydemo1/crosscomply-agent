"""Official pages that only issue an attached instrument, pages printed twice, and handshakes old servers need."""

from __future__ import annotations

import ssl
import urllib.error

import pytest

from law_agent.data.cleaners.common import clean_text
from law_agent.data.html_content import attachment_links, extract_main_text
from law_agent.kb import enrichment
from law_agent.kb.enrichment import attachment_title, process_enrichment_job
from law_agent.kb.source_llm import read_with_model

NOTICE = """<html><body><div class="nav">首页 政策</div>
<div class="pages_content mhide"><p>国家发展改革委 国家数据局关于印发《公共数据资源登记管理暂行办法》的通知</p>
<p>现将《公共数据资源登记管理暂行办法》印发给你们，请遵照执行。</p>
<p>附件：<a href="./P0201.pdf">《公共数据资源登记管理暂行办法》.pdf</a> <a href="./P0202.zip">《公共数据资源登记管理暂行办法》.ofd</a></p></div>
<div class="pages_content pchide"><p>国家发展改革委 国家数据局关于印发《公共数据资源登记管理暂行办法》的通知</p>
<p>现将《公共数据资源登记管理暂行办法》印发给你们，请遵照执行。</p></div>
<div class="footer"><a href="/about.pdf">网站说明.pdf</a></div></body></html>"""


def test_a_page_printed_twice_for_desktop_and_mobile_keeps_one_copy():
    text = extract_main_text(NOTICE)
    assert text.count("现将《公共数据资源登记管理暂行办法》印发给你们") == 1
    assert "首页" not in text


def test_attachment_links_are_read_from_the_main_text_and_made_absolute():
    links = attachment_links(NOTICE, "https://www.gov.cn/zhengce/zhengceku/202501/content_1.htm")
    assert links == [("《公共数据资源登记管理暂行办法》.pdf", "https://www.gov.cn/zhengce/zhengceku/202501/P0201.pdf"),
                     ("《公共数据资源登记管理暂行办法》.ofd", "https://www.gov.cn/zhengce/zhengceku/202501/P0202.zip")]
    assert attachment_title("附件1：证券期货业网络和信息安全管理办法.pdf") == "证券期货业网络和信息安全管理办法"
    assert attachment_title("2. 中国（江苏）自由贸易试验区数据出境管理清单（负面清单）（2025版）") == \
        "中国（江苏）自由贸易试验区数据出境管理清单（负面清单）（2025版）"


def test_a_pages_filing_card_and_tools_are_not_indexed():
    page = ("<table><tr><td>索 引 号</td><td>bm56000001</td><td>发文字号</td><td>证监会令【第218号】</td></tr></table>\n"
            "发文字号：工信部网安〔2024〕82号\n成文日期：2024-05-10\n发布时间：2024-05-24 09:03 来源：网络安全管理局\n"
            "第一条 为了规范数据处理活动，制定本办法。\n【返回顶部】\n【关闭窗口】\n《数据安全管理办法》.pdf")
    cleaned = clean_text(page).text
    assert cleaned.strip() == "第一条 为了规范数据处理活动，制定本办法。"


class _Store:
    def __init__(self):
        self.expanded, self.transitions, self.draft = None, [], None

    def record_raw(self, *_args, **_kwargs):
        pass

    def record_draft(self, _job_id, draft):
        self.draft = draft

    def expand(self, job_id, *, attachments, context_text, published_date):
        self.expanded = (job_id, attachments, context_text, published_date)
        return ["enrich_child"]

    def transition(self, _job_id, *, status, source=None, error=None, **_kwargs):
        self.transitions.append((status, error))


class _Service:
    def __init__(self, corpus):
        self.corpus, self.ingested = corpus, None

    def list_sources(self):
        return []

    def ingest_file(self, source, _path):
        self.ingested = source


def _job(url="https://www.gov.cn/zhengce/zhengceku/202501/content_1.htm"):
    return {"id": "enrich_notice", "title": "关于印发《公共数据资源登记管理暂行办法》的通知", "url": url,
            "raw_path": None, "raw_sha256": None, "source_json": None, "attempt_count": 1,
            "published_date": "2025-01-08"}


def test_a_notice_that_only_issues_an_attachment_is_followed_to_it_and_never_indexed(monkeypatch, tmp_path):
    monkeypatch.setattr(enrichment, "_download", lambda _url, path: path.write_text(NOTICE, encoding="utf-8"))
    store, service = _Store(), _Service(tmp_path)
    process_enrichment_job(_job(), store=store, service=service, client=None)
    _job_id, attachments, context, published = store.expanded
    assert attachments == [("公共数据资源登记管理暂行办法", "https://www.gov.cn/zhengce/zhengceku/202501/P0201.pdf")]
    assert "印发给你们" in context and published == "2025-01-08"
    assert service.ingested is None and store.transitions == [] and store.draft is None


def test_a_notice_whose_attachments_cannot_be_parsed_fails_with_the_reason(monkeypatch, tmp_path):
    page = NOTICE.replace('<a href="./P0201.pdf">《公共数据资源登记管理暂行办法》.pdf</a>', "")
    monkeypatch.setattr(enrichment, "_download", lambda _url, path: path.write_text(page, encoding="utf-8"))
    store, service = _Store(), _Service(tmp_path)
    process_enrichment_job(_job(), store=store, service=service, client=None)
    status, error = store.transitions[-1]
    assert status == "failed" and "正文在附件中" in error and store.expanded is None and service.ingested is None


def test_a_page_carrying_its_own_articles_is_not_replaced_by_its_annex(monkeypatch, tmp_path):
    page = ("<html><body><div><p>银行保险机构数据安全管理办法</p>"
            + "".join(f"<p>第{n}条 银行保险机构应当履行数据安全保护义务。</p>" for n in "一二三")
            + '<p>附件：<a href="./annex.pdf">数据安全事件分级</a></p></div></body></html>')
    monkeypatch.setattr(enrichment, "_download", lambda _url, path: path.write_text(page, encoding="utf-8"))
    store, service = _Store(), _Service(tmp_path)
    process_enrichment_job(_job(), store=store, service=service, client=None)
    assert store.expanded is None and store.transitions[-1][0] == "awaiting_review"


def test_an_attachment_is_read_with_the_notice_that_issued_it():
    class Client:
        def chat_json(self, messages):
            self.prompt = messages[-1].content
            return {"issuing_body": {"value": "国家发展和改革委员会", "quote": "国家发展改革委 国家数据局关于印发"},
                    "publish_date": {"value": "2025-01-08", "quote": "2025年1月8日"}}

    client = Client()
    reading = read_with_model(client=client, source_id="s", title="公共数据资源登记管理暂行办法", text="第一条 为了规范登记。",
                              url="https://www.gov.cn/P0201.pdf", source_site="www.gov.cn", file_format="pdf",
                              context="国家发展改革委 国家数据局关于印发《公共数据资源登记管理暂行办法》的通知\n2025年1月8日")
    assert "issuing_notice" in client.prompt and "关于印发" in client.prompt
    assert "publish_date" not in reading.rejected and reading.inference.source.publish_date == "2025-01-08"


def test_a_handshake_an_old_server_refuses_is_retried_with_the_curves_it_accepts(monkeypatch):
    attempts = []

    def urlopen(_request, timeout, context):
        attempts.append(context)
        if len(attempts) < 3:
            raise urllib.error.URLError(ssl.SSLError(1, "[SSL: BAD_ECPOINT] bad ecpoint"))
        return "response"

    monkeypatch.setattr(enrichment.urllib.request, "urlopen", urlopen)
    assert enrichment._open("https://mzt.hunan.gov.cn/x.html") == "response"
    assert len(attempts) == 3 and all(context.verify_mode == ssl.CERT_REQUIRED for context in attempts)


def test_a_refusal_that_is_not_a_handshake_failure_is_not_retried(monkeypatch):
    attempts = []

    def urlopen(_request, timeout, context):
        attempts.append(context)
        raise urllib.error.HTTPError("https://x.gov.cn", 403, "Forbidden", {}, None)

    monkeypatch.setattr(enrichment.urllib.request, "urlopen", urlopen)
    with pytest.raises(urllib.error.HTTPError):
        enrichment._open("https://x.gov.cn/a.html")
    assert len(attempts) == 1


def test_a_new_document_type_rechunks_the_stored_original(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from law_agent.data.schemas import SourceRecord
    from law_agent.kb.admin import KnowledgeBaseAdminService

    raw = tmp_path / "raw" / "tpl" / "source.pdf"
    raw.parent.mkdir(parents=True)
    raw.write_bytes(b"%PDF")
    old = SourceRecord(source_id="tpl", title="个人信息出境标准合同（范本）", source_url="https://www.cac.gov.cn/a",
                       source_site="cac.gov.cn", doc_type="guideline", authority="ministry_policy",
                       citation_role="implementation_reference", issuing_body="国家互联网信息办公室",
                       publish_date="2023-02-24")
    service = KnowledgeBaseAdminService(tmp_path)
    calls = []
    monkeypatch.setattr(service, "list_sources", lambda: [SimpleNamespace(source=old)])
    monkeypatch.setattr(service, "ingest_file", lambda source, path: calls.append((source.doc_type, path)) or {"action": "replace"})
    assert service.update_source_metadata(old.model_copy(update={"doc_type": "contract"})) == {"action": "replace"}
    assert calls == [("contract", raw)]


def test_a_notice_whose_attachment_link_is_not_in_the_page_fails_instead_of_indexing_the_notice(monkeypatch, tmp_path):
    page = ("<html><body><div><p>工业和信息化部关于印发《工业和信息化领域数据安全风险评估实施细则（试行）》的通知</p>"
            "<p>现将《工业和信息化领域数据安全风险评估实施细则（试行）》印发给你们，请认真遵照执行。</p></div></body></html>")
    monkeypatch.setattr(enrichment, "_download", lambda _url, path: path.write_text(page, encoding="utf-8"))
    store, service = _Store(), _Service(tmp_path)
    process_enrichment_job(_job(), store=store, service=service, client=None)
    status, error = store.transitions[-1]
    assert status == "failed" and "没有可入库的附件" in error and service.ingested is None


class _Model:
    def __init__(self, answer):
        self.answer, self.prompt = answer, None

    def chat_json(self, messages):
        self.prompt = messages[-1].content
        return self.answer


ISSUING = "现将《公共数据资源登记管理暂行办法》印发给你们"


def test_the_model_decides_the_page_only_issues_files_and_which_of_them_to_index(monkeypatch, tmp_path):
    monkeypatch.setattr(enrichment, "_download", lambda _url, path: path.write_text(NOTICE, encoding="utf-8"))
    store, service = _Store(), _Service(tmp_path)
    model = _Model({"page_kind": {"value": "issuing_notice", "quote": ISSUING},
                    "attachments": [{"index": 1, "kind": "被印发的文件", "follow": True},
                                    {"index": 2, "kind": "同一文件的其他格式版本", "follow": False}]})
    process_enrichment_job(_job(), store=store, service=service, client=model)
    assert '"attachments"' in model.prompt and "P0201.pdf" not in model.prompt  # names and formats, not addresses
    assert store.expanded[1] == [("公共数据资源登记管理暂行办法", "https://www.gov.cn/zhengce/zhengceku/202501/P0201.pdf")]
    assert service.ingested is None


def test_a_page_the_model_reads_as_the_document_itself_is_not_replaced_by_its_attachments(monkeypatch, tmp_path):
    monkeypatch.setattr(enrichment, "_download", lambda _url, path: path.write_text(NOTICE, encoding="utf-8"))
    store, service = _Store(), _Service(tmp_path)
    model = _Model({"page_kind": {"value": "document", "quote": ISSUING},
                    "doc_type": {"value": "policy", "quote": ISSUING}})
    process_enrichment_job(_job(), store=store, service=service, client=model)
    assert store.expanded is None and store.transitions[-1][0] == "awaiting_review"
    assert store.draft["page_kind"] == "document" and len(store.draft["attachments"]) == 2


def test_a_page_the_model_is_unsure_about_waits_for_the_reviewer_with_its_attachments(monkeypatch, tmp_path):
    monkeypatch.setattr(enrichment, "_download", lambda _url, path: path.write_text(NOTICE, encoding="utf-8"))
    store, service = _Store(), _Service(tmp_path)
    model = _Model({"page_kind": {"value": "issuing_notice", "quote": ISSUING},
                    "attachments": [{"index": 1, "kind": "被印发的文件", "follow": True}],
                    "uncertain": ["page_kind"], "note": "页面可能还有正文未展示"})
    process_enrichment_job(_job(), store=store, service=service, client=model)
    assert store.expanded is None and store.transitions[-1][0] == "awaiting_review"
    assert "page_kind" in store.draft["unresolved"]
    assert store.draft["attachments"][0] == {"name": "《公共数据资源登记管理暂行办法》.pdf",
                                             "url": "https://www.gov.cn/zhengce/zhengceku/202501/P0201.pdf", "follow": True}
