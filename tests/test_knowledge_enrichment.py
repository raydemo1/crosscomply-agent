from __future__ import annotations

import hashlib
from types import SimpleNamespace

from law_agent.data.schemas import SourceRecord
from law_agent.kb import enrichment
from law_agent.kb.enrichment import process_enrichment_job
from law_agent.kb.source_inference import infer_source
from law_agent.kb.enrichment_worker import KnowledgeEnrichmentWorker
from law_agent.kb.service import normalized_content_hash


class _Model:
    """A model client that returns one fixed reading."""

    def __init__(self, answer):
        self.answer, self.calls = answer, 0

    def chat_json(self, _messages):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _reads(*, doc_type, authority, role, issuer, effective=(None, None), publish=(None, None), uncertain=(), note=None):
    """Each field as (value, quote)."""
    pairs = {"doc_type": doc_type, "authority": authority, "citation_role": role, "issuing_body": issuer,
             "effective_date": effective, "publish_date": publish}
    return _Model({name: {"value": value, "quote": quote} for name, (value, quote) in pairs.items()}
                  | {"uncertain": list(uncertain), "note": note})


NPC = "全国人民代表大会常务委员会"


def _law_reading(effective: str, entry: str):
    return _reads(doc_type=("law", NPC), authority=("national_law", NPC), role=("primary_legal_basis", NPC),
                  issuer=(NPC, NPC), effective=(effective, entry))


def _infer(title: str, text: str, url: str):
    return infer_source(source_id="legal_x", title=title, text=text, url=url, source_site="example",
                        file_format="html")


def test_national_law_is_settled_from_its_title_and_entry_into_force() -> None:
    title = "中华人民共和国示例法"
    inference = _infer(title, f"{title}\n第一条 示例。\n本法自2026年10月1日起施行。", "https://flk.npc.gov.cn/detail?id=abc")
    assert not inference.unresolved and not inference.settled  # the rules only prefill
    assert (inference.source.doc_type, inference.source.authority) == ("law", "national_law")
    assert inference.source.citation_role == "primary_legal_basis"
    assert inference.source.effective_date == "2026-10-01"
    undated = _infer(title, f"{title}\n第一条 示例。", "https://flk.npc.gov.cn/detail?id=abc")
    assert undated.unresolved == ["effective_date"]


def test_policy_notice_is_drafted_but_not_a_citable_norm() -> None:
    title = "数据出境政策通知"
    inference = _infer(title, f"{title}\n本通知自2026年10月1日起施行。", "https://www.cac.gov.cn/notice.html")
    assert inference.source.doc_type == "policy"
    assert inference.source.citation_role == "implementation_reference"


def test_departmental_rule_is_read_from_either_order_form() -> None:
    title = "数据出境管理办法"
    for order in ("国家互联网信息办公室第十二号令", "国家互联网信息办公室令\n第16号"):
        inference = _infer(title, f"{title}\n{order}\n第一条 示例。\n本办法自2026年10月1日起施行。",
                           "https://www.cac.gov.cn/2026-08/20/c_1.htm")
        assert not inference.unresolved, order
        assert inference.source.authority == "departmental_rule"
        assert inference.source.issuing_body == "国家互联网信息办公室"
        assert inference.source.publish_date == "2026-08-20"
    # A rule named further down is a citation of another instrument, not this one's enacting order.
    quoted = _infer("关于数据出境的公告", "关于数据出境的公告\n依据《促进和规范数据跨境流动规定》（国家互联网信息办公室令第16号）。",
                    "https://www.cac.gov.cn/notice.html")
    assert quoted.source.doc_type == "policy"


def test_regional_list_and_amended_law_are_inferred_for_review() -> None:
    negative_list = _infer("中国（浙江）自由贸易试验区数据出境管理清单（负面清单）（2024版）_浙江网信网", "清单正文",
                           "https://www.zjwx.gov.cn/2024-05/08/c_1.htm")
    assert negative_list.source.title == "中国（浙江）自由贸易试验区数据出境管理清单（负面清单）（2024版）"
    assert (negative_list.source.doc_type, negative_list.source.authority) == ("policy", "local_normative_document")
    assert negative_list.source.citation_role == "conditional_local_basis"
    assert negative_list.source.applicable_region == "CN-ZJ"
    assert negative_list.source.instrument_key == "中国（浙江）自由贸易试验区数据出境管理清单（负面清单）"
    amended = _infer("中华人民共和国网络安全法", "中华人民共和国网络安全法\n（2016年11月7日通过　根据2025年10月28日《关于修改〈中华人民共和国网络安全法〉的决定》修正）\n第一条 示例。\n本法自2017年6月1日起施行。",
                     "https://flk.npc.gov.cn/detail?id=csl")
    assert amended.unresolved == ["effective_date"]


def test_independent_enrichment_worker_does_not_start_review_agent() -> None:
    class Store:
        def __init__(self):
            self.sent = None

        def claim_notifications(self):
            return [{"job_id": "job", "case_id": "case", "title": "新规", "url": "https://www.cac.gov.cn/new"}]

        def mark_notification(self, *_args, **kwargs):
            self.sent = kwargs

        def claim_approved(self, _worker_id):
            return None

        def claim(self, _worker_id):
            return None

    class Messenger:
        def send_text_message(self, *, open_id, text):
            assert open_id == "ou_reviewer"
            assert "新规" in text
            return "om_123"

    store = Store()
    worker = KnowledgeEnrichmentWorker(
        store=store, service=None, worker_id="test",
        notification_client=Messenger(), notification_open_id="ou_reviewer",
    )
    assert worker.run_once()
    assert store.sent == {"sent": True, "message_id": "om_123"}


def test_official_law_is_parsed_staged_and_sent_to_kb(monkeypatch, tmp_path) -> None:
    title = "中华人民共和国示例法"
    body = (
        f"{title}\n（2026年6月1日第十四届全国人民代表大会常务委员会第二十次会议通过）\n"
        "第一条 " + "规范示例事项。" * 35 + "\n"
        "第二条 " + "适用示例程序。" * 35 + "\n"
        "第三条 " + "保障示例权益。" * 35 + "\n"
        "本法自2026年10月1日起施行。"
    )
    raw = body.encode("utf-8")

    def download(_url, path):
        path.write_bytes(raw)

    class Store:
        recorded = None
        transition_result = None

        def record_raw(self, _job_id, *, path, sha256, parsed_excerpt):
            self.recorded = (path, sha256, parsed_excerpt)

        def record_draft(self, _job_id, draft):
            self.draft = draft

        def transition(self, _job_id, *, status, source=None, **_kwargs):
            self.transition_result = (status, source)

    class Service:
        corpus = tmp_path
        ingested = None

        def list_sources(self):
            return []

        def ingest_file(self, source, path):
            self.ingested = (source, path.read_bytes())

    monkeypatch.setattr(enrichment, "_download", download)
    store, service = Store(), Service()
    job = {
        "id": "enrich_example", "title": title,
        "url": "https://flk.npc.gov.cn/detail?id=example.txt",
        "raw_path": None, "raw_sha256": None, "source_json": None,
        "attempt_count": 1,
    }
    process_enrichment_job(job, store=store, service=service,
                           client=_law_reading("2026-10-01", "本法自2026年10月1日起施行"))

    assert store.transition_result[0] == "published"
    assert store.transition_result[1].citation_role == "primary_legal_basis"
    assert service.ingested[0].source_id == store.transition_result[1].source_id
    assert service.ingested[1] == raw
    assert store.recorded[1] == hashlib.sha256(raw).hexdigest()
    assert "第三条" in store.recorded[2]


def test_guideline_waits_for_approval_and_uses_staged_original(monkeypatch, tmp_path) -> None:
    raw = "数据出境办事指南\n申请材料以官方页面为准。".encode()
    downloads = []

    def download(_url, path):
        downloads.append(path)
        path.write_bytes(raw)

    class Store:
        def __init__(self):
            self.recorded = None
            self.transitions = []

        def record_raw(self, _job_id, *, path, sha256, parsed_excerpt):
            self.recorded = (path, sha256, parsed_excerpt)

        def record_draft(self, _job_id, draft):
            self.draft = draft

        def transition(self, _job_id, *, status, source=None, **_kwargs):
            self.transitions.append((status, source))

    class Service:
        corpus = tmp_path
        ingested = None

        def list_sources(self):
            return []

        def ingest_file(self, source, path):
            self.ingested = (source, path.read_bytes())

    monkeypatch.setattr(enrichment, "_download", download)
    store, service = Store(), Service()
    job = {
        "id": "enrich_guide", "title": "数据出境办事指南",
        "url": "https://www.cac.gov.cn/guide.txt",
        "raw_path": None, "raw_sha256": None, "source_json": None,
        "attempt_count": 1,
    }
    process_enrichment_job(job, store=store, service=service, client=None)
    assert store.transitions == [("awaiting_review", None)]
    assert service.ingested is None
    assert store.draft["reader"] == "rules" and "未配置模型" in store.draft["evidence"]["reader"]

    approved = SourceRecord(
        source_id="legal_guide", title=job["title"], source_url=job["url"],
        source_site="cac.gov.cn", doc_type="guideline", authority="ministry_policy",
        citation_role="implementation_reference", file_format="txt",
    )
    job.update({
        "raw_path": str(store.recorded[0]),
        "raw_sha256": store.recorded[1],
        "source_json": approved.model_dump(mode="json"),
    })
    process_enrichment_job(job, store=store, service=service, client=None)
    assert downloads == [store.recorded[0]]
    assert service.ingested[1] == raw
    assert store.transitions[-1][0] == "published"
    assert store.transitions[-1][1].citation_role == "implementation_reference"


def test_same_official_body_does_not_create_version(monkeypatch, tmp_path) -> None:
    title = "中华人民共和国示例法"
    body = f"{title}\n第一条 旧版正文。\n本法自2020年1月1日起施行。"
    existing = SourceRecord(
        source_id="old", title=title, source_url="https://flk.npc.gov.cn/law",
        source_site="flk.npc.gov.cn", doc_type="law", authority="national_law",
        effective_date="2020-01-01", instrument_key=None,
    )

    class Store:
        status = None
        source = None

        def record_raw(self, *_args, **_kwargs):
            pass

        def record_draft(self, _job_id, draft):
            self.draft = draft

        def transition(self, _job_id, *, status, source=None, **_kwargs):
            self.status = status
            self.source = source

    class Service:
        corpus = tmp_path

        def list_sources(self):
            return [SimpleNamespace(source=existing)]

        def get_source(self, _source_id):
            return {"content_hash": normalized_content_hash(body)}

        def is_up_to_date(self, *_args):
            return True

        def ingest_file(self, *_args):
            raise AssertionError("unchanged body must not be ingested")

    monkeypatch.setattr(enrichment, "_download", lambda _url, path: path.write_text(body, encoding="utf-8"))
    store = Store()
    process_enrichment_job({
        "id": "enrich_same", "title": title, "url": existing.source_url,
        "raw_path": None, "raw_sha256": None, "source_json": None, "attempt_count": 1,
    }, store=store, service=Service(), client=None)
    assert store.status == "unchanged"
    assert store.source.source_id == "old"


def test_same_body_on_a_stale_index_is_republished_not_reported_unchanged(
    monkeypatch, tmp_path
) -> None:
    """A matching content hash alone is not currency.

    The stored body can be identical while the published chunks, the processing
    signature or the source metadata moved. Reporting the job as unchanged then
    left the index stale under a body that had never been re-chunked.
    """

    title = "中华人民共和国示例法"
    body = (f"{title}\n{NPC}\n" + "第一条 测试规定。" * 40 + "\n" + "第二条 测试条件。" * 40
            + "\n本法自2020年1月1日起施行。")
    existing = SourceRecord(
        source_id="old", title=title, source_url="https://flk.npc.gov.cn/law",
        source_site="flk.npc.gov.cn", doc_type="law", authority="national_law",
        effective_date="2019-01-01", instrument_key=None,
        citation_role="primary_legal_basis",
    )

    class Store:
        def __init__(self):
            self.statuses: list[str] = []

        def record_raw(self, *_args, **_kwargs):
            pass

        def record_draft(self, _job_id, draft):
            self.draft = draft

        def transition(self, _job_id, *, status, source=None, **_kwargs):
            self.statuses.append(status)

    class Service:
        corpus = tmp_path
        ingested = None

        def list_sources(self):
            return [SimpleNamespace(source=existing)]

        def get_source(self, _source_id):
            return {"content_hash": normalized_content_hash(body)}

        def is_up_to_date(self, *_args):
            return False

        def ingest_file(self, source, path):
            self.ingested = source

        def update_source_metadata(self, source):
            self.retired = source

    monkeypatch.setattr(enrichment, "_download", lambda _url, path: path.write_text(body, encoding="utf-8"))
    store, service = Store(), Service()
    process_enrichment_job({
        "id": "enrich_stale", "title": title, "url": existing.source_url,
        "raw_path": None, "raw_sha256": None, "source_json": None, "attempt_count": 1,
    }, store=store, service=service, client=_law_reading("2020-01-01", "本法自2020年1月1日起施行"))
    assert service.ingested is not None
    assert store.statuses[-1] == "published"


def test_changed_same_url_with_same_effective_date_waits_for_review(monkeypatch, tmp_path) -> None:
    title = "中华人民共和国示例法"
    body = (f"{title}\n{NPC}\n" + "第一条 新规定。" * 40 + "\n"
            + "第二条 新条件。" * 40 + "\n" + "第三条 新要求。" * 40
            + "\n本法自2020年1月1日起施行。")
    old = SourceRecord(
        source_id="old", title=title, source_url="https://flk.npc.gov.cn/law",
        source_site="flk.npc.gov.cn", doc_type="law", authority="national_law",
        effective_date="2020-01-01", citation_role="primary_legal_basis",
    )

    class Store:
        status = None

        def record_raw(self, *_args, **_kwargs):
            pass

        def record_draft(self, _job_id, draft):
            self.draft = draft

        def transition(self, _job_id, *, status, **_kwargs):
            self.status = status

    class Service:
        corpus = tmp_path

        def list_sources(self):
            return [SimpleNamespace(source=old)]

        def get_source(self, _source_id):
            return {"content_hash": normalized_content_hash("旧版正文")}

        def ingest_file(self, *_args):
            raise AssertionError("same effective date needs manual review")

    monkeypatch.setattr(enrichment, "_download", lambda _url, path: path.write_text(body, encoding="utf-8"))
    store = Store()
    process_enrichment_job({
        "id": "enrich_changed", "title": title, "url": old.source_url,
        "raw_path": None, "raw_sha256": None, "source_json": None, "attempt_count": 1,
    }, store=store, service=Service(), client=_law_reading("2020-01-01", "本法自2020年1月1日起施行"))
    assert store.status == "awaiting_review"


def test_newer_same_url_law_keeps_legacy_instrument_group(monkeypatch, tmp_path) -> None:
    title = "中华人民共和国示例法"
    body = (f"{title}\n{NPC}\n" + "第一条 新规定。" * 40 + "\n"
            + "第二条 新条件。" * 40 + "\n" + "第三条 新要求。" * 40
            + "\n本法自2026年10月1日起施行。")
    old = SourceRecord(
        source_id="old", title=title, source_url="https://flk.npc.gov.cn/law",
        source_site="flk.npc.gov.cn", doc_type="law", authority="national_law",
        effective_date="2020-01-01", citation_role="primary_legal_basis",
    )

    class Store:
        status = None
        source = None

        def record_raw(self, *_args, **_kwargs):
            pass

        def record_draft(self, _job_id, draft):
            self.draft = draft

        def transition(self, _job_id, *, status, source=None, **_kwargs):
            self.status, self.source = status, source

    class Service:
        corpus = tmp_path
        ingested = None

        def list_sources(self):
            return [SimpleNamespace(source=old)]

        def get_source(self, _source_id):
            return {"content_hash": normalized_content_hash("旧版正文")}

        def ingest_file(self, source, _path):
            self.ingested = source

        def update_source_metadata(self, source):
            self.retired = source

    monkeypatch.setattr(enrichment, "_download", lambda _url, path: path.write_text(body, encoding="utf-8"))
    store, service = Store(), Service()
    process_enrichment_job({
        "id": "enrich_newer", "title": title, "url": old.source_url,
        "raw_path": None, "raw_sha256": None, "source_json": None, "attempt_count": 1,
    }, store=store, service=service, client=_law_reading("2026-10-01", "本法自2026年10月1日起施行"))
    assert store.status == "published"
    assert store.source.source_id != old.source_id
    assert service.ingested.instrument_key == old.title
    # The edition it replaces ends where the new one takes effect.
    assert (service.retired.source_id, service.retired.valid_to) == ("old", "2026-10-01")


def _fake_store():
    class Store:
        def __init__(self):
            self.transitions, self.draft = [], None

        def record_raw(self, *_args, **_kwargs):
            pass

        def record_draft(self, _job_id, draft):
            self.draft = draft

        def transition(self, _job_id, *, status, source=None, error=None, **_kwargs):
            self.transitions.append((status, source, error))

    return Store()


def _fake_service(tmp_path, published):
    class Service:
        corpus = tmp_path

        def __init__(self):
            self.ingested, self.updated = None, []

        def list_sources(self):
            return [SimpleNamespace(source=item) for item in published]

        def get_source(self, _source_id):
            return {"content_hash": "other"}

        def ingest_file(self, source, _path):
            self.ingested = source

        def update_source_metadata(self, source):
            self.updated.append(source)

    return Service()


def test_new_edition_at_a_new_address_publishes_and_retires_the_old_one(monkeypatch, tmp_path) -> None:
    title = "数据出境安全评估办法（2026修订）"
    body = (f"{title}\n国家互联网信息办公室令\n第30号\n" + "".join(f"第{n}条 规定内容。\n" for n in "一二三四")
            + "本办法自2026年10月1日起施行。")
    old = SourceRecord(source_id="old", title="数据出境安全评估办法", source_url="https://www.cac.gov.cn/2022-07/07/c_1.htm",
                       source_site="cac.gov.cn", doc_type="regulation", authority="departmental_rule",
                       citation_role="primary_legal_basis", effective_date="2022-09-01")
    monkeypatch.setattr(enrichment, "_download", lambda _url, path: path.write_text(body, encoding="utf-8"))
    store, service = _fake_store(), _fake_service(tmp_path, [old])
    process_enrichment_job({"id": "enrich_new", "title": f"{title}_中国政府网",
                            "url": "https://www.cac.gov.cn/2026-08/20/c_2.htm", "raw_path": None, "raw_sha256": None,
                            "source_json": None, "attempt_count": 1}, store=store, service=service,
                           client=_reads(doc_type=("regulation", "国家互联网信息办公室令\n第30号"),
                                   authority=("departmental_rule", "国家互联网信息办公室令\n第30号"),
                                   role=("primary_legal_basis", "国家互联网信息办公室令"),
                                   issuer=("国家互联网信息办公室", "国家互联网信息办公室令"),
                                   effective=("2026-10-01", "本办法自2026年10月1日起施行")))

    assert store.transitions[-1][0] == "published"
    assert service.ingested.title == title and service.ingested.instrument_key == "数据出境安全评估办法"
    assert [(item.source_id, item.valid_to, item.instrument_key) for item in service.updated] == [
        ("old", "2026-10-01", "数据出境安全评估办法")]
    assert store.draft["supersedes"] == [{"source_id": "old", "title": "数据出境安全评估办法", "date": "2022-09-01"}]


def test_an_edition_not_later_than_the_published_one_waits_with_the_reason(monkeypatch, tmp_path) -> None:
    title = "数据出境安全评估办法"
    body = f"{title}\n国家互联网信息办公室令\n第11号\n" + "".join(f"第{n}条 内容。\n" for n in "一二三") + "本办法自2022年9月1日起施行。"
    old = SourceRecord(source_id="old", title=title, source_url="https://www.cac.gov.cn/a.htm", source_site="cac.gov.cn",
                       doc_type="regulation", authority="departmental_rule", effective_date="2022-09-01")
    monkeypatch.setattr(enrichment, "_download", lambda _url, path: path.write_text(body, encoding="utf-8"))
    store, service = _fake_store(), _fake_service(tmp_path, [old])
    process_enrichment_job({"id": "enrich_same_date", "title": title, "url": "https://www.cac.gov.cn/b.htm",
                            "raw_path": None, "raw_sha256": None, "source_json": None, "attempt_count": 1},
                           store=store, service=service, client=_reads(doc_type=("regulation", "国家互联网信息办公室令\n第11号"),
                                   authority=("departmental_rule", "国家互联网信息办公室令\n第11号"),
                                   role=("primary_legal_basis", "国家互联网信息办公室令"),
                                   issuer=("国家互联网信息办公室", "国家互联网信息办公室令"),
                                   effective=("2022-09-01", "本办法自2022年9月1日起施行")))
    status, _source, error = store.transitions[-1]
    assert status == "awaiting_review" and "未晚于" in error
    assert service.ingested is None and service.updated == []


def _rule_job(job_id: str):
    return {"id": job_id, "title": "数据出境管理办法", "url": "https://www.cac.gov.cn/2026-02/03/c_3.htm",
            "raw_path": None, "raw_sha256": None, "source_json": None, "attempt_count": 1}


RULE_BODY = ("数据出境管理办法\n（2026年2月1日 国家互联网信息办公室）\n"
             + "".join(f"第{n}条 规定内容。\n" for n in "一二三四") + "本办法自2026年3月1日起施行。")


def test_a_field_the_model_is_unsure_of_waits_for_the_reviewer_with_its_note(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(enrichment, "_download", lambda _url, path: path.write_text(RULE_BODY, encoding="utf-8"))
    store, service = _fake_store(), _fake_service(tmp_path, [])
    note = "正文没有令号，无法确定是部门规章还是规范性文件"
    process_enrichment_job(_rule_job("enrich_unsure"), store=store, service=service, client=_reads(
        doc_type=("regulation", "数据出境管理办法"), authority=("departmental_rule", None),
        role=("primary_legal_basis", "数据出境管理办法"), issuer=("国家互联网信息办公室", "国家互联网信息办公室"),
        effective=("2026-03-01", "本办法自2026年3月1日起施行"), uncertain=["doc_type", "authority"], note=note))
    assert store.transitions[-1][0] == "awaiting_review" and service.ingested is None
    draft = store.draft
    assert draft["reader"] == "model"
    assert set(draft["unresolved"]) >= {"doc_type", "authority"}
    assert draft["evidence"]["note"] == note
    assert draft["source"]["doc_type"] == "regulation"  # the best guess stays as the reviewer's prefill
    assert draft["chunks"] and draft["chunk_count"] >= 1


def test_a_failed_model_call_falls_back_to_the_rules_and_never_publishes(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(enrichment, "_download", lambda _url, path: path.write_text(RULE_BODY, encoding="utf-8"))
    store, service = _fake_store(), _fake_service(tmp_path, [])
    process_enrichment_job(_rule_job("enrich_down"), store=store, service=service,
                           client=_Model(ConnectionError("model down")))
    assert store.transitions[-1][0] == "awaiting_review" and service.ingested is None
    assert store.draft["reader"] == "rules"
    assert "模型读取失败" in store.draft["evidence"]["reader"]
