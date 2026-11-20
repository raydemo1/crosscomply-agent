"""Build actionable work from the existing case and remediation state machines."""

from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from law_agent.review.case_store import CaseStore, UserRecord
from law_agent.review.dates import business_today
from law_agent.review.impact import REASON_LABELS, RegulationImpact
from law_agent.review.portfolio import Portfolio, crosses_threshold
from law_agent.review.transfer_store import APPROVED, CLOSED
from law_agent.review.workflow import review_has_missing_information

CASE_WORK_STATES = (
    "draft", "needs_info", "pending_review", "review_running",
    "pending_source_verification", "pending_feishu_approval", "run_failed",
)
REMINDER_WINDOW = timedelta(days=2)


def configured_reviewer(users: list[UserRecord]) -> UserRecord | None:
    username = os.getenv("CROSSCOMPLY_WORK_REVIEWER_USERNAME", "").strip().casefold()
    return next((user for user in users if user.role != "requester" and user.username.casefold() == username), None)


class WorkInbox:
    def __init__(self, cases: CaseStore, enterprise: Any, governance: Any, transfers: Any,
                 portfolio: Portfolio, impact: RegulationImpact) -> None:
        self.cases = cases
        self.enterprise = enterprise
        self.governance = governance
        self.transfers = transfers
        self.portfolio = portfolio
        self.impact = impact

    def list(self, user: UserRecord, *, today: date | None = None) -> list[dict[str, Any]]:
        today = today or business_today()
        users = {record.id: record for record in self.cases.list_assignable_users()}
        reviewer = configured_reviewer(list(users.values()))
        can_review = user.role != "requester"
        tasks = self.cases.list_remediation_tasks(**({} if can_review else {"assignee_id": user.id}))
        cases = {row["id"]: row for row in self.cases.list_cases(user, statuses=CASE_WORK_STATES)}
        plans: dict[str, dict[str, Any] | None] = {}
        items: list[dict[str, Any]] = []
        impacts = self.impact.visible_flags(status="open") if can_review else []
        blocked = {(row["case_id"], row["task_id"]) for row in impacts if row["severity"] == "blocking"}

        def plan_for(case_id: str) -> dict[str, Any] | None:
            if case_id not in plans:
                plans[case_id] = self.cases.get_remediation_plan(case_id)
            return plans[case_id]

        def add(
            kind: str, title: str, context: str, reason: str, action: str, href: str,
            *, key: str, assignee_id: str | None = None, team: bool = False,
            exclude: str | None = None, due_on: str | None = None, on_ready: bool = True,
        ) -> None:
            if team:
                if not can_review or user.id == exclude:
                    return
                assignee_id = reviewer.id if reviewer and reviewer.id != exclude else None
            elif assignee_id != user.id:
                return
            assignee = users.get(assignee_id or "")
            items.append({
                "id": f"{kind}:{key}", "kind": kind, "title": title, "context": context,
                "reason": reason, "action": action, "href": href, "due_on": due_on,
                "owner_name": assignee.display_name if assignee else "法务审核组",
                "notification_user_id": assignee_id if assignee else None, "notify_on_ready": on_ready,
                "urgency": ("overdue" if due_on and due_on < today.isoformat() else
                            "soon" if due_on and due_on <= (today + REMINDER_WINDOW).isoformat() else None),
            })

        for task in tasks:
            if not task.get("is_current", True) or task["status"] == "completed":
                continue
            case_id = task["case_id"]
            case = cases.get(case_id) or self.cases.get_case(case_id)
            plan = plan_for(case_id)
            if not case or not plan or plan["status"] == "cancelled" or case["status"] == "rejected":
                continue
            if task["phase"] == "post_approval" and case["status"] not in APPROVED:
                continue
            href = f"/reviews/remediation?task={task['id']}"
            title, context = task["title"], case["title"]
            full_task = next((entry for entry in plan["tasks"] if entry["id"] == task["id"]), task)
            submission = (full_task.get("submissions") or [None])[-1]
            if task["status"] == "pending_review" and can_review:
                if submission and submission["status"] == "pending_review":
                    add("remediation_review", title, context, "负责人已提交处理说明", "验收事项", href,
                        key=submission["id"], team=True, exclude=submission["submitted_by"])
            elif task["status"] in {"open", "in_progress"}:
                assignee = task.get("assignee_id")
                key = f"{task['id']}:{submission['id'] if submission else 'new'}"
                if assignee in users:
                    fact = task.get("task_kind") == "fact_confirmation"
                    add("supplement" if fact else "remediation", title, context,
                        "需要核实一项业务事实" if fact else "待提交处理说明或证明材料",
                        "补充事实" if fact else "处理事项", href, key=key, assignee_id=assignee, due_on=task.get("due_date"))
                elif can_review:
                    add("remediation", title, context, "需要指定事项负责人", "分派事项", href,
                        key=f"{key}:unassigned", team=True)

        for identifier, case in cases.items():
            status = case["status"]
            owner = case.get("owner_id") or case["created_by"]
            title = case["title"]
            url = f"/cases/{identifier}"
            if status == "draft":
                if self.transfers.review_for_case(identifier):
                    add("transfer_draft", title, "", "请核对变更事实并补充本次材料", "继续申报",
                        f"/workbench?case={identifier}", key=identifier, assignee_id=owner)
                continue
            task = self.enterprise.get_latest_task(identifier)
            task_id = task.id if task else identifier
            if task and task.status == "waiting_input":
                state = task.agent_state or {}
                add("supplement", title, "", "审查正在等待申报人回答", "回答问题", f"{url}?action=answer",
                    key=f"{task_id}:{state.get('gate_id') or 'input'}", assignee_id=owner)
                continue
            if task and task.status in {"queued", "running"}:
                continue
            if status == "pending_review":
                add("review", title, "", "材料已提交，审查任务等待入队", "打开案件", url, key=task_id, team=True)
            elif status == "run_failed":
                add("review", title, "", "上一次审查未完成", "检查审查任务", url,
                    key=f"{task_id}:failed:{task.attempt_count if task else 0}", team=True)
            elif status == "pending_source_verification":
                add("source_review", title, "", "当前结论的法规依据待人工复核", "核对法规依据", url,
                    key=task_id, team=True)
            elif status in {"needs_info", "pending_feishu_approval"}:
                plan = plan_for(identifier)
                current = [row for row in (plan or {}).get("tasks") or []
                           if row.get("is_current", True) and row.get("phase") == "pre_approval"]
                if any(row.get("blocking") and row["status"] != "completed" for row in current):
                    continue
                if status == "needs_info":
                    if plan and (current or task and task.status == "succeeded"):
                        add("supplement", title, "", "现有必办事项已处理，可按最新材料重新审查",
                            "查看案件并重审", url, key=f"{task_id}:resubmit", assignee_id=owner)
                    elif not plan:
                        add("supplement", title, "", "案件仍缺少已确认的业务信息", "查看需补充内容", url,
                            key=f"{task_id}:facts", assignee_id=owner)
                    continue
                if (not can_review or task is None or task.status != "succeeded"
                        or review_has_missing_information(task.result or {})
                        or self.governance.get_latest_approval(identifier) or (identifier, task_id) in blocked):
                    continue
                transfer = self.transfers.review_for_case(identifier)
                if transfer and (transfer.get("impact") or {}).get("task_id") != task_id:
                    add("impact_review", title, "", "本次变更需要法务确认影响", "查看前后差异",
                        f"/cases/business/{transfer['activity_id']}", key=task_id, team=True)
                else:
                    add("approval", title, "", "审查和送审前必办事项已完成", "发起飞书审批", url,
                        key=task_id, team=True)

        for activity in self.transfers.list(user):
            if activity["owner_id"] != user.id:
                continue
            due_on = activity["next_review_on"].isoformat()
            if (due_on <= (today + REMINDER_WINDOW).isoformat()
                    and all(row["status"] in CLOSED for row in self.transfers.reviews(activity["id"]))):
                add("transfer_due", activity["title"], "", "请核对当前业务和上次审批范围",
                    "查看出境业务", f"/cases/business/{activity['id']}", key=f"{activity['id']}:{due_on}",
                    assignee_id=user.id, due_on=due_on, on_ready=False)

        if can_review:
            for flag in impacts:
                due = flag["effective_on"]
                add("regulation_impact", flag["case_title"], flag["title"], REASON_LABELS[flag["reason"]],
                    "处理法规变更提示", f"/governance?tab=impact&flag={flag['id']}", key=flag["id"], team=True,
                    due_on=due.isoformat() if due and flag["severity"] == "upcoming" else None,
                    on_ready=flag["severity"] != "upcoming")
            for summary in self.portfolio.summaries(user, today.year):
                if not crosses_threshold(summary) or self.portfolio.acked(summary.entity_key, today.year, summary.band_upper):
                    continue
                count = sum(row.status == "counted" for row in summary.contributions)
                reason = (
                    f"{count} 项业务合计（未去重）：非敏感 {summary.non_sensitive_sum:,}、敏感 {summary.sensitive_sum:,}；"
                    f"单项最高：非敏感 {summary.non_sensitive_max:,}、敏感 {summary.sensitive_max:,}"
                )
                add("portfolio_threshold", f"{summary.entity_name} 当年累计可能跨档", "", reason,
                    "核对主体累计", f"/cases/business?{urlencode({'entity': summary.entity_key})}",
                    key=f"{summary.entity_key}:{today.year}:{summary.band_upper}", team=True)

        return sorted(items, key=lambda item: (item["due_on"] or "9999-12-31", item["kind"], item["title"]))
