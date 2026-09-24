"""Add a superseded terminal state for review tasks and attempts.

A paused Agent run (``waiting_input``) is only meaningful for the frozen material and rule
snapshot it was started from.  Once the user changes those inputs the old question is stale,
so the run needs a terminal state that is neither success nor failure.

Revision ID: 0009_review_task_superseded
Revises: 0008_remediation_assessment
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0009_review_task_superseded"
down_revision: str | None = "0008_remediation_assessment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TASK_STATUSES = "('queued', 'running', 'waiting_input', 'succeeded', 'failed', 'superseded')"
ATTEMPT_STATUSES = "('running', 'waiting_input', 'succeeded', 'failed', 'superseded')"


def upgrade() -> None:
    op.drop_constraint("review_tasks_status_ck", "review_tasks", type_="check")
    op.create_check_constraint("review_tasks_status_ck", "review_tasks", f"status IN {TASK_STATUSES}")
    op.drop_constraint("review_task_attempts_status_ck", "review_task_attempts", type_="check")
    op.create_check_constraint(
        "review_task_attempts_status_ck", "review_task_attempts", f"status IN {ATTEMPT_STATUSES}"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE review_task_attempts SET status = 'failed' WHERE status = 'superseded'"
    )
    op.execute("UPDATE review_tasks SET status = 'failed' WHERE status = 'superseded'")
    op.drop_constraint("review_task_attempts_status_ck", "review_task_attempts", type_="check")
    op.create_check_constraint(
        "review_task_attempts_status_ck",
        "review_task_attempts",
        "status IN ('running', 'waiting_input', 'succeeded', 'failed')",
    )
    op.drop_constraint("review_tasks_status_ck", "review_tasks", type_="check")
    op.create_check_constraint(
        "review_tasks_status_ck",
        "review_tasks",
        "status IN ('queued', 'running', 'waiting_input', 'succeeded', 'failed')",
    )