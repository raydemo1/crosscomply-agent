"""Create the single-enterprise CrossComply baseline.

Revision ID: 0001_enterprise_baseline
Revises: None
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_enterprise_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CASE_STATUS = (
    "draft",
    "needs_info",
    "pending_review",
    "review_running",
    "pending_feishu_approval",
    "approved",
    "conditionally_approved",
    "rejected",
    "run_failed",
)


def _timestamps() -> tuple[sa.Column, sa.Column]:
    return (
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    json_default = sa.text("'{}'::jsonb")

    op.create_table(
        "users",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("username", sa.Text(), nullable=False, unique=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.true(), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("role IN ('requester', 'reviewer', 'admin')", name="users_role_ck"),
    )
    op.create_table(
        "sessions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "user_id", sa.Text(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("token_hash", sa.Text(), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("sessions_token_idx", "sessions", ["token_hash"])

    op.create_table(
        "review_cases",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("case_number", sa.Text(), nullable=False, unique=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("material_text", sa.Text(), nullable=False),
        sa.Column("material_source", sa.Text()),
        sa.Column("intake_json", postgresql.JSONB(), server_default=json_default, nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("review_mode", sa.Text(), server_default="llm", nullable=False),
        sa.Column("rerank_mode", sa.Text(), server_default="off", nullable=False),
        sa.Column("created_by", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("owner_id", sa.Text(), sa.ForeignKey("users.id")),
        sa.Column("facts_confirmed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("risk_level", sa.Text()),
        sa.Column("trace_id", sa.Text()),
        sa.Column("response_json", postgresql.JSONB()),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN (" + ", ".join(repr(status) for status in CASE_STATUS) + ")",
            name="review_cases_status_ck",
        ),
    )
    op.create_index("review_cases_created_by_idx", "review_cases", ["created_by"])
    op.create_index("review_cases_status_idx", "review_cases", ["status"])

    op.create_table(
        "materials",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "case_id",
            sa.Text(),
            sa.ForeignKey("review_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("logical_name", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        *_timestamps(),
        sa.UniqueConstraint("case_id", "logical_name", name="materials_case_name_uq"),
    )
    op.create_table(
        "material_versions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "material_id",
            sa.Text(),
            sa.ForeignKey("materials.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False, unique=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("uploaded_by", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("parse_status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("parser", sa.Text()),
        sa.Column("parser_version", sa.Text()),
        sa.Column("parsed_text", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("material_id", "version_number", name="material_versions_number_uq"),
        sa.CheckConstraint(
            "parse_status IN ('pending', 'parsing', 'ready', 'failed')",
            name="material_versions_parse_status_ck",
        ),
    )
    op.create_index("material_versions_sha256_idx", "material_versions", ["sha256"])

    op.create_table(
        "material_snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "case_id",
            sa.Text(),
            sa.ForeignKey("review_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("case_id", "fingerprint", name="material_snapshots_fingerprint_uq"),
    )
    op.create_table(
        "material_snapshot_versions",
        sa.Column(
            "snapshot_id",
            sa.Text(),
            sa.ForeignKey("material_snapshots.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "material_version_id",
            sa.Text(),
            sa.ForeignKey("material_versions.id"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.UniqueConstraint("snapshot_id", "position", name="material_snapshot_position_uq"),
    )
    op.create_table(
        "rule_snapshots",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "case_id",
            sa.Text(),
            sa.ForeignKey("review_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "material_snapshot_id",
            sa.Text(),
            sa.ForeignKey("material_snapshots.id"),
            nullable=False,
        ),
        sa.Column("ruleset_version", sa.Text(), nullable=False),
        sa.Column("facts_json", postgresql.JSONB(), nullable=False),
        sa.Column("determination_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "review_tasks",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "case_id",
            sa.Text(),
            sa.ForeignKey("review_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "material_snapshot_id",
            sa.Text(),
            sa.ForeignKey("material_snapshots.id"),
            nullable=False,
        ),
        sa.Column(
            "rule_snapshot_id", sa.Text(), sa.ForeignKey("rule_snapshots.id"), nullable=False
        ),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False, unique=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="queued"),
        sa.Column("current_node", sa.Text()),
        sa.Column("error_category", sa.Text()),
        sa.Column("error_message", sa.Text()),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("model_id", sa.Text()),
        sa.Column(
            "data_boundary_summary_json",
            postgresql.JSONB(),
            server_default=json_default,
            nullable=False,
        ),
        sa.Column("result_json", postgresql.JSONB()),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="review_tasks_status_ck",
        ),
    )
    op.create_index("review_tasks_claim_idx", "review_tasks", ["status", "created_at"])
    op.create_index(
        "review_tasks_one_active_per_case_uq",
        "review_tasks",
        ["case_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_table(
        "review_task_attempts",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("review_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("worker_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("failed_node", sa.Text()),
        sa.Column("error_category", sa.Text()),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("task_id", "attempt_number", name="review_task_attempts_number_uq"),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="review_task_attempts_status_ck",
        ),
    )

    op.create_table(
        "case_actions",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "case_id",
            sa.Text(),
            sa.ForeignKey("review_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("owner_role", sa.Text(), server_default="reviewer", nullable=False),
        sa.Column("priority", sa.Text(), server_default="medium", nullable=False),
        sa.Column("status", sa.Text(), server_default="open", nullable=False),
        sa.Column("due_date", sa.Date()),
        *_timestamps(),
    )
    op.create_table(
        "case_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "case_id",
            sa.Text(),
            sa.ForeignKey("review_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actor_id", sa.Text(), sa.ForeignKey("users.id")),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("from_status", sa.Text()),
        sa.Column("to_status", sa.Text()),
        sa.Column("payload_json", postgresql.JSONB(), server_default=json_default, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("case_events_case_idx", "case_events", ["case_id", "created_at"])

    op.create_table(
        "case_feedback",
        sa.Column(
            "case_id",
            sa.Text(),
            sa.ForeignKey("review_cases.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("actor_id", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("conclusion_useful", sa.Boolean()),
        sa.Column("missing_sources", sa.Text(), server_default="", nullable=False),
        sa.Column("notes", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "citation_verdicts_json",
            postgresql.JSONB(),
            server_default=json_default,
            nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    op.create_table(
        "approval_records",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "case_id",
            sa.Text(),
            sa.ForeignKey("review_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("review_tasks.id"), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False, server_default="feishu"),
        sa.Column("instance_id", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("approver_name", sa.Text()),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("payload_json", postgresql.JSONB(), server_default=json_default, nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'conditionally_approved', 'rejected', 'withdrawn')",
            name="approval_records_status_ck",
        ),
    )
    op.create_table(
        "approval_delivery_jobs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "case_id",
            sa.Text(),
            sa.ForeignKey("review_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("task_id", sa.Text(), sa.ForeignKey("review_tasks.id"), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False, unique=True),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("instance_id", sa.Text()),
        sa.Column("error_message", sa.Text()),
        *_timestamps(),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'failed', 'succeeded')",
            name="approval_delivery_jobs_status_ck",
        ),
    )
    op.create_index(
        "approval_delivery_jobs_lease_idx",
        "approval_delivery_jobs",
        ["status", "lease_expires_at"],
    )
    op.create_table(
        "approval_events",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "approval_id",
            sa.Text(),
            sa.ForeignKey("approval_records.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_event_id", sa.Text(), nullable=False, unique=True),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("signature_valid", sa.Boolean(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("payload_json", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "report_records",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "case_id",
            sa.Text(),
            sa.ForeignKey("review_cases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("approval_id", sa.Text(), sa.ForeignKey("approval_records.id"), nullable=False),
        sa.Column("object_key", sa.Text(), nullable=False, unique=True),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("metadata_json", postgresql.JSONB(), server_default=json_default, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )


def downgrade() -> None:
    for table in (
        "report_records",
        "approval_events",
        "approval_delivery_jobs",
        "approval_records",
        "case_feedback",
        "case_events",
        "case_actions",
        "review_task_attempts",
        "review_tasks",
        "rule_snapshots",
    ):
        op.drop_table(table)
    op.drop_constraint("review_cases_current_snapshot_fk", "review_cases", type_="foreignkey")
    for table in (
        "material_snapshot_versions",
        "material_snapshots",
        "material_versions",
        "materials",
        "review_cases",
        "sessions",
        "users",
    ):
        op.drop_table(table)
