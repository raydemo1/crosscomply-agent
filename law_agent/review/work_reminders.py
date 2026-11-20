"""Deliver actionable work reminders to explicitly mapped Feishu accounts."""

from __future__ import annotations

import json
import logging
import os
from uuid import NAMESPACE_URL, UUID, uuid5

import psycopg

from law_agent.review.case_store import PostgresCaseStore
from law_agent.review.enterprise_store import PostgresEnterpriseStore
from law_agent.review.feishu import FeishuApprovalClient
from law_agent.review.governance_store import PostgresGovernanceStore
from law_agent.review.portfolio import Portfolio
from law_agent.review.transfer_store import PostgresTransferStore
from law_agent.review.work_inbox import WorkInbox

LOG = logging.getLogger(__name__)


class WorkReminderWorker:
    def __init__(self, dsn: str, client: FeishuApprovalClient, base_url: str, recipients: dict[str, str]) -> None:
        self.dsn = dsn
        self.client = client
        self.base_url = base_url.rstrip("/")
        self.recipients = recipients
        self.cases = PostgresCaseStore(dsn)
        enterprise = PostgresEnterpriseStore(dsn)
        governance = PostgresGovernanceStore(dsn)
        transfers = PostgresTransferStore(dsn)
        self.inbox = WorkInbox(self.cases, enterprise, governance, transfers,
                               Portfolio(transfers, self.cases, enterprise, governance))

    def _claim(self, key: UUID) -> bool:
        with psycopg.connect(self.dsn) as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO work_reminder_deliveries (id, retry_after) VALUES (%s, now() + interval '5 minutes')
                   ON CONFLICT (id) DO UPDATE SET retry_after = EXCLUDED.retry_after
                   WHERE work_reminder_deliveries.sent_at IS NULL AND work_reminder_deliveries.retry_after <= now()
                   RETURNING id""",
                (key,),
            )
            return cur.fetchone() is not None

    def _sent(self, key: UUID) -> None:
        with psycopg.connect(self.dsn) as conn:
            conn.execute("UPDATE work_reminder_deliveries SET sent_at = now() WHERE id = %s", (key,))

    def run_once(self) -> None:
        for user in self.cases.list_assignable_users():
            open_id = self.recipients.get(user.username.casefold())
            if not open_id:
                continue
            for item in self.inbox.list(user):
                if item["notification_user_id"] != user.id:
                    continue
                due = item["due_on"] if item["urgency"] else None
                if not due and not item["notify_on_ready"]:
                    continue
                key = uuid5(NAMESPACE_URL, f"crosscomply:work:{user.id}:{item['id']}:{due or 'ready'}")
                if not self._claim(key):
                    continue
                # Keep business names and case materials in the authenticated workspace.
                deadline = f"\n截止日期：{due}" if due else ""
                try:
                    self.client.send_text_message(
                        open_id=open_id, uuid=str(key),
                        text=f"CrossComply · {item['action']}\n{item['reason']}{deadline}\n{self.base_url}{item['href']}",
                    )
                    self._sent(key)
                except Exception:  # noqa: BLE001 - retry delivery without disrupting enrichment work
                    LOG.warning("待办提醒发送失败，将重试（投递 %s）", key)


def configured_recipients() -> dict[str, str]:
    raw = os.getenv("CROSSCOMPLY_FEISHU_WORK_RECIPIENTS", "").strip()
    if not raw:
        return {}
    try:
        values = json.loads(raw)
        if not isinstance(values, dict) or not all(
            isinstance(value, str) and value.strip().startswith("ou_") for value in values.values()
        ):
            raise ValueError("invalid mapping")
        return {username.strip().casefold(): open_id.strip() for username, open_id in values.items()}
    except ValueError:
        LOG.error("CROSSCOMPLY_FEISHU_WORK_RECIPIENTS 格式无效，未启用待办提醒")
        return {}
