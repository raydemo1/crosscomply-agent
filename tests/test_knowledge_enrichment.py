from __future__ import annotations

import hashlib

from law_agent.data.schemas import SourceRecord
from law_agent.kb import enrichment
from law_agent.kb.enrichment import process_enrichment_job, verified_auto_source
from law_agent.kb.enrichment_worker import KnowledgeEnrichmentWorker


def test_national_law_requires_official_url_title_and_effective_date() -> None:
    title = "中华人民共和国示例法"
    text = f"{title}\n第一条 示例。\n本法自2026年10月1日起施行。"
    source = verified_auto_source(title, text, "https://flk.npc.gov.cn/detail?id=abc")
    assert source is not None
    assert source.authority == "national_law"
    assert source.citation_role == "primary_legal_basis"
    assert source.effective_date == "2026-10-01"
    assert verified_auto_source(title, text, "https://flk.npc.gov.cn.evil.com/detail") is None
    assert verified_auto_source(title, text.replace("自2026年10月1日起施行", ""), source.source_url) is None


def test_policy_notice_never_auto_publishes() -> None:
    title = "数据出境政策通知"
    text = f"{title}\n本通知自2026年10月1日起施行。"
    assert verified_auto_source(title, text, "https://www.cac.gov.cn/notice.html") is None


def test_departmental_rule_requires_order_number() -> None:
    title = "数据出境管理办法"
    text = f"{title}\n国家互联网信息办公室第十二号令\n本办法自2026年10月1日起施行。"
    source = verified_auto_source(title, text, "https://www.cac.gov.cn/order.html")
    assert source is not None
    assert source.authority == "departmental_rule"
    assert verified_auto_source(title, text.replace("第十二号令", ""), source.source_url) is None


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
        f"{title}\n"
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

        def transition(self, _job_id, *, status, source=None, **_kwargs):
            self.transition_result = (status, source)

    class Service:
        corpus = tmp_path
        ingested = None

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
    process_enrichment_job(job, store=store, service=service)

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

        def transition(self, _job_id, *, status, source=None, **_kwargs):
            self.transitions.append((status, source))

    class Service:
        corpus = tmp_path
        ingested = None

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
    process_enrichment_job(job, store=store, service=service)
    assert store.transitions == [("awaiting_review", None)]
    assert service.ingested is None

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
    process_enrichment_job(job, store=store, service=service)
    assert downloads == [store.recorded[0]]
    assert service.ingested[1] == raw
    assert store.transitions[-1][0] == "published"
    assert store.transitions[-1][1].citation_role == "implementation_reference"
