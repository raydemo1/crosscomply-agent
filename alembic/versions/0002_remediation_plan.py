"""Create the independent remediation-plan workflow and remove case actions."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002_remediation_plan"
down_revision: str | None = "0001_enterprise_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    json_default = sa.text("'{}'::jsonb")
    op.drop_table("case_actions")

    op.create_table(
        "remediation_plans",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "case_id", sa.Text(), sa.ForeignKey("review_cases.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("created_by", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="draft"),
        sa.Column("no_remediation_reason", sa.Text()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("case_id", name="remediation_plans_case_uq"),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'completed', 'cancelled')",
            name="remediation_plans_status_ck",
        ),
    )
    op.create_index("remediation_plans_status_idx", "remediation_plans", ["status"])

    op.create_table(
        "remediation_tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "plan_id", sa.Text(), sa.ForeignKey("remediation_plans.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "case_id", sa.Text(), sa.ForeignKey("review_cases.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("acceptance_criteria", sa.Text(), server_default="", nullable=False),
        sa.Column("source_recommendation_index", sa.Integer()),
        sa.Column("source_recommendation", sa.Text()),
        sa.Column("assignee_id", sa.Text(), sa.ForeignKey("users.id")),
        sa.Column("priority", sa.Text(), nullable=False, server_default="medium"),
        sa.Column("due_date", sa.Date()),
        sa.Column("status", sa.Text(), nullable=False, server_default="open"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("priority IN ('high', 'medium', 'low')", name="remediation_tasks_priority_ck"),
        sa.CheckConstraint(
            "status IN ('open', 'in_progress', 'pending_review', 'completed')",
            name="remediation_tasks_status_ck",
        ),
    )
    op.create_index("remediation_tasks_assignee_idx", "remediation_tasks", ["assignee_id", "status"])
    op.create_index("remediation_tasks_plan_idx", "remediation_tasks", ["plan_id", "created_at"])

    op.create_table(
        "remediation_submissions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "task_id", sa.Text(), sa.ForeignKey("remediation_tasks.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("submitted_by", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending_review"),
        sa.Column("reviewed_by", sa.Text(), sa.ForeignKey("users.id")),
        sa.Column("review_note", sa.Text()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending_review', 'accepted', 'rejected')",
            name="remediation_submissions_status_ck",
        ),
    )
    op.create_index("remediation_submissions_task_idx", "remediation_submissions", ["task_id", "created_at"])

    op.create_table(
        "remediation_evidence",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "submission_id", sa.Text(), sa.ForeignKey("remediation_submissions.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("uri", sa.Text()),
        sa.Column("object_key", sa.Text()),
        sa.Column("content_type", sa.Text()),
        sa.Column("sha256", sa.String(length=64)),
        sa.Column("byte_size", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('case_material', 'file', 'link')", name="remediation_evidence_kind_ck"),
    )
    op.create_index("remediation_evidence_submission_idx", "remediation_evidence", ["submission_id"])

    op.create_table(
        "remediation_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "plan_id", sa.Text(), sa.ForeignKey("remediation_plans.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("remediation_tasks.id", ondelete="CASCADE")),
        sa.Column("actor_id", sa.Text(), sa.ForeignKey("users.id")),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), server_default=json_default, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("remediation_events_plan_idx", "remediation_events", ["plan_id", "created_at"])


def downgrade() -> None:
    for table in (
        "remediation_events",
        "remediation_evidence",
        "remediation_submissions",
        "remediation_tasks",
        "remediation_plans",
    ):
        op.drop_table(table)
    op.create_table(
        "case_actions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("case_id", sa.Text(), sa.ForeignKey("review_cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("owner_role", sa.Text(), server_default="reviewer", nullable=False),
        sa.Column("priority", sa.Text(), server_default="medium", nullable=False),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("due_date", sa.Date()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
