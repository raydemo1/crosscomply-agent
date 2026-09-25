"""Persist official-source enrichment and case recheck tasks.

Revision ID: 0010_knowledge_enrichment
Revises: 0009_review_task_superseded
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010_knowledge_enrichment"
down_revision: str | None = "0009_review_task_superseded"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("review_cases_status_ck", "review_cases", type_="check")
    op.create_check_constraint(
        "review_cases_status_ck", "review_cases",
        "status IN ('draft', 'needs_info', 'pending_review', 'review_running', "
        "'pending_source_verification', 'pending_feishu_approval', 'approved', "
        "'conditionally_approved', 'rejected', 'run_failed')",
    )
    op.create_table(
        "knowledge_enrichment_jobs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("canonical_url", sa.Text(), nullable=False),
        sa.Column("candidate_hash", sa.String(length=64), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("source_json", postgresql.JSONB()),
        sa.Column("source_id", sa.Text()),
        sa.Column("raw_path", sa.Text()),
        sa.Column("raw_sha256", sa.String(length=64)),
        sa.Column("parsed_excerpt", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("worker_id", sa.Text()),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'awaiting_review', 'approved', 'published', 'rejected', 'failed')",
            name="knowledge_enrichment_jobs_status_ck",
        ),
        sa.UniqueConstraint("canonical_url", "candidate_hash", name="knowledge_enrichment_candidate_uq"),
    )
    op.create_index("knowledge_enrichment_jobs_status_idx", "knowledge_enrichment_jobs", ["status"])
    op.create_table(
        "knowledge_enrichment_cases",
        sa.Column("job_id", sa.Text(), sa.ForeignKey("knowledge_enrichment_jobs.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("case_id", sa.Text(), sa.ForeignKey("review_cases.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("review_task_id", sa.Text(), sa.ForeignKey("review_tasks.id", ondelete="SET NULL")),
        sa.Column("material", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("recheck_status", sa.Text(), nullable=False, server_default="not_required"),
        sa.Column("notification_status", sa.Text(), nullable=False, server_default="not_required"),
        sa.Column("notification_error", sa.Text()),
        sa.Column("notification_message_id", sa.Text()),
        sa.Column("notification_retry_at", sa.DateTime(timezone=True)),
        sa.Column("notification_lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "recheck_status IN ('not_required', 'waiting_source', 'pending', 'done')",
            name="knowledge_enrichment_cases_status_ck",
        ),
        sa.CheckConstraint(
            "notification_status IN ('not_required', 'pending', 'sending', 'sent')",
            name="knowledge_enrichment_cases_notification_ck",
        ),
    )
    op.create_index("knowledge_enrichment_cases_case_idx", "knowledge_enrichment_cases", ["case_id"])


def downgrade() -> None:
    op.drop_table("knowledge_enrichment_cases")
    op.drop_table("knowledge_enrichment_jobs")
    op.execute("UPDATE review_cases SET status='needs_info' WHERE status='pending_source_verification'")
    op.drop_constraint("review_cases_status_ck", "review_cases", type_="check")
    op.create_check_constraint(
        "review_cases_status_ck", "review_cases",
        "status IN ('draft', 'needs_info', 'pending_review', 'review_running', "
        "'pending_feishu_approval', 'approved', 'conditionally_approved', 'rejected', 'run_failed')",
    )
