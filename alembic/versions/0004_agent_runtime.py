"""Persist the single-Agent runtime and human-input pauses."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004_agent_runtime"
down_revision: str | None = "0003_case_templates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("review_tasks", sa.Column("agent_state_json", postgresql.JSONB()))
    op.drop_constraint("review_tasks_status_ck", "review_tasks", type_="check")
    op.create_check_constraint(
        "review_tasks_status_ck",
        "review_tasks",
        "status IN ('queued', 'running', 'waiting_input', 'succeeded', 'failed')",
    )
    op.drop_constraint("review_task_attempts_status_ck", "review_task_attempts", type_="check")
    op.create_check_constraint(
        "review_task_attempts_status_ck",
        "review_task_attempts",
        "status IN ('running', 'waiting_input', 'succeeded', 'failed')",
    )
    op.drop_index("review_tasks_one_active_per_case_uq", table_name="review_tasks")
    op.create_index(
        "review_tasks_one_active_per_case_uq",
        "review_tasks",
        ["case_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running', 'waiting_input')"),
    )
    op.create_table(
        "review_task_steps",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("review_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_number", sa.Integer(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "observation_json",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("task_id", "step_number", name="review_task_steps_number_uq"),
    )
    op.create_index(
        "review_task_steps_task_idx", "review_task_steps", ["task_id", "step_number"]
    )


def downgrade() -> None:
    op.execute(
        "UPDATE review_task_attempts SET status = 'failed', "
        "error_category = 'migration_downgrade', "
        "error_message = 'waiting_input is unavailable after downgrade', "
        "finished_at = COALESCE(finished_at, now()) "
        "WHERE status = 'waiting_input'"
    )
    op.execute(
        "UPDATE review_tasks SET status = 'failed', "
        "error_category = 'migration_downgrade', "
        "error_message = 'waiting_input is unavailable after downgrade', "
        "updated_at = now() WHERE status = 'waiting_input'"
    )
    op.drop_index("review_task_steps_task_idx", table_name="review_task_steps")
    op.drop_table("review_task_steps")
    op.drop_index("review_tasks_one_active_per_case_uq", table_name="review_tasks")
    op.create_index(
        "review_tasks_one_active_per_case_uq",
        "review_tasks",
        ["case_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.drop_constraint("review_task_attempts_status_ck", "review_task_attempts", type_="check")
    op.create_check_constraint(
        "review_task_attempts_status_ck",
        "review_task_attempts",
        "status IN ('running', 'succeeded', 'failed')",
    )
    op.drop_constraint("review_tasks_status_ck", "review_tasks", type_="check")
    op.create_check_constraint(
        "review_tasks_status_ck",
        "review_tasks",
        "status IN ('queued', 'running', 'succeeded', 'failed')",
    )
    op.drop_column("review_tasks", "agent_state_json")
