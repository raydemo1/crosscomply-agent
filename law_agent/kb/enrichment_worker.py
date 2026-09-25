"""Independent knowledge-enrichment worker. It never runs the review model."""

from __future__ import annotations

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
    ) -> None:
        self.store = store
        self.service = service
        self.worker_id = worker_id
        self.notification_client = notification_client
        self.notification_open_id = notification_open_id

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
            process_enrichment_job(job, store=self.store, service=self.service)
            processed = True
        return processed


def main() -> None:
    from pathlib import Path

    from law_agent.config import load_service_config
    from law_agent.review.feishu import FeishuApprovalClient, FeishuApprovalConfig
    from law_agent.review.retrieval.corpus import DEFAULT_CHUNKS_PATH

    config = load_service_config()
    open_id = os.getenv("CROSSCOMPLY_FEISHU_RECHECK_OPEN_ID", "")
    notification_client = None
    if open_id and os.getenv("CROSSCOMPLY_FEISHU_APP_ID") and os.getenv("CROSSCOMPLY_FEISHU_APP_SECRET"):
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
    )
    poll_seconds = max(0.2, float(os.getenv("CROSSCOMPLY_WORKER_POLL_SECONDS", "2")))
    while True:
        if not worker.run_once():
            time.sleep(poll_seconds)


if __name__ == "__main__":
    main()
