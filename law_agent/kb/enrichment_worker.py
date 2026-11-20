"""Independent knowledge-enrichment worker. It never runs the review model."""

from __future__ import annotations

import logging
import os
import socket
import time
from typing import Protocol

from law_agent.kb.admin import KnowledgeBaseAdminService
from law_agent.kb.enrichment import PostgresEnrichmentStore, process_enrichment_job


class MessageClient(Protocol):
    def send_text_message(self, *, open_id: str, text: str) -> str: ...


class KnowledgeEnrichmentWorker:
    def __init__(
        self, *, store: PostgresEnrichmentStore, service: KnowledgeBaseAdminService,
        worker_id: str, notification_client: MessageClient | None = None,
        notification_open_id: str = "",
        impact=None,
    ) -> None:
        self.store = store
        self.service = service
        self.worker_id = worker_id
        self.notification_client = notification_client
        self.notification_open_id = notification_open_id
        self.impact = impact

    def run_once(self) -> bool:
        processed = False
        if self.notification_client and self.notification_open_id:
            for notice in self.store.claim_notifications():
                processed = True
                try:
                    message_id = self.notification_client.send_text_message(
                        open_id=self.notification_open_id,
                        text=(
                            f"案件 {notice['case_id']} 的官方法源已完成核验，待复核原结论。"
                            f"\n{notice['title']}\n{notice['url']}"
                        ),
                    )
                    self.store.mark_notification(
                        notice["job_id"], notice["case_id"], sent=True, message_id=message_id,
                    )
                except Exception as exc:  # noqa: BLE001 - notification failures must remain retryable
                    self.store.mark_notification(
                        notice["job_id"], notice["case_id"], sent=False, error=str(exc)[:1000],
                    )
        job = self.store.claim_approved(self.worker_id) or self.store.claim(self.worker_id)
        if job is not None:
            process_enrichment_job(job, store=self.store, service=self.service, impact=self.impact)
            processed = True
        return processed


def main() -> None:
    from pathlib import Path

    from law_agent.config import load_service_config
    from law_agent.review.case_store import PostgresCaseStore
    from law_agent.review.dates import business_today
    from law_agent.review.enterprise_store import PostgresEnterpriseStore
    from law_agent.review.feishu import FeishuApprovalClient, FeishuApprovalConfig
    from law_agent.review.governance_store import PostgresGovernanceStore
    from law_agent.review.impact import RegulationImpact
    from law_agent.review.impact_store import PostgresImpactStore
    from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH
    from law_agent.review.transfer_store import PostgresTransferStore
    from law_agent.review.work_reminders import WorkReminderWorker, configured_recipients

    config = load_service_config()
    dsn = config.postgres.dsn
    impacts = PostgresImpactStore(dsn)
    impact = RegulationImpact(
        impacts, PostgresCaseStore(dsn), PostgresEnterpriseStore(dsn), PostgresGovernanceStore(dsn), PostgresTransferStore(dsn),
    )
    open_id = os.getenv("CROSSCOMPLY_FEISHU_RECHECK_OPEN_ID", "")
    recipients = configured_recipients()
    public_url = os.getenv("CROSSCOMPLY_PUBLIC_BASE_URL", "").strip()
    notification_client = None
    if (open_id or recipients) and os.getenv("CROSSCOMPLY_FEISHU_APP_ID") and os.getenv("CROSSCOMPLY_FEISHU_APP_SECRET"):
        import httpx

        notification_client = FeishuApprovalClient(
            FeishuApprovalConfig(
                app_id=os.environ["CROSSCOMPLY_FEISHU_APP_ID"],
                app_secret=os.environ["CROSSCOMPLY_FEISHU_APP_SECRET"],
                approval_code="", verification_token="", encrypt_key="",
            ),
            httpx.request,
        )
    worker = KnowledgeEnrichmentWorker(
        store=PostgresEnrichmentStore(config.postgres.dsn),
        service=KnowledgeBaseAdminService(Path(DEFAULT_CHUNKS_PATH).parent),
        worker_id=os.getenv("CROSSCOMPLY_ENRICHMENT_WORKER_ID") or f"{socket.gethostname()}-{os.getpid()}",
        notification_client=notification_client,
        notification_open_id=open_id,
        impact=impact,
    )
    reminder_worker = (
        WorkReminderWorker(config.postgres.dsn, notification_client, public_url, recipients)
        if notification_client and recipients and public_url.startswith(("http://", "https://")) else None
    )
    next_reminder_check = 0.0
    next_impact_check = 0.0
    last_impact_date = None
    poll_seconds = max(0.2, float(os.getenv("CROSSCOMPLY_WORKER_POLL_SECONDS", "2")))
    while True:
        today = business_today()
        if last_impact_date != today and time.monotonic() >= next_impact_check:
            try:
                impacts.promote(today)
                last_impact_date = today
            except Exception:
                logging.getLogger(__name__).exception("法规变更生效扫描失败")
                next_impact_check = time.monotonic() + 60
        if reminder_worker and time.monotonic() >= next_reminder_check:
            try:
                reminder_worker.run_once()
            except Exception:
                logging.getLogger(__name__).exception("待办提醒检查失败")
            next_reminder_check = time.monotonic() + 60
        if not worker.run_once():
            time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
