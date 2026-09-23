"""Link remediation tasks to review issues and persist Agent re-reviews."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008_remediation_assessment"
down_revision: str | None = "0007_review_annotations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("remediation_tasks", sa.Column("source_review_result_id", sa.Text()))
    op.add_column("remediation_tasks", sa.Column("source_issue_id", sa.Text()))

    op.add_column("remediation_evidence", sa.Column("parsed_text", sa.Text()))
    op.add_column(
        "remediation_evidence",
        sa.Column("parse_status", sa.Text(), nullable=False, server_default="pending"),
    )
    op.create_check_constraint(
        "remediation_evidence_parse_status_ck",
        "remediation_evidence",
        "parse_status IN ('pending', 'ready', 'failed', 'not_applicable')",
    )

    op.create_table(
        "remediation_assessments",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column(
            "task_id",
            sa.Text(),
            sa.ForeignKey("remediation_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "submission_id",
            sa.Text(),
            sa.ForeignKey("remediation_submissions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_review_result_id", sa.Text()),
        sa.Column("source_issue_id", sa.Text()),
        sa.Column("run_status", sa.Text(), nullable=False, server_default="running"),
        sa.Column("status", sa.Text()),
        sa.Column("summary", sa.Text()),
        sa.Column(
            "confirmed_points_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "remaining_gaps_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("next_request", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "grounded_evidence_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("gate_id", sa.Text()),
        sa.Column("question", sa.Text()),
        sa.Column("error_message", sa.Text()),
        sa.Column("trace_id", sa.Text()),
        sa.Column("agent_state_json", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "run_status IN ('running', 'waiting_input', 'completed', 'failed')",
            name="remediation_assessments_run_status_ck",
        ),
        sa.CheckConstraint(
            "status IS NULL OR status IN "
            "('resolved', 'partially_resolved', 'not_resolved', 'insufficient_evidence')",
            name="remediation_assessments_status_ck",
        ),
    )
    op.create_index(
        "remediation_assessments_task_idx",
        "remediation_assessments",
        ["task_id", "created_at"],
    )
    op.create_index(
        "remediation_assessments_submission_idx",
        "remediation_assessments",
        ["submission_id"],
    )


def downgrade() -> None:
    op.drop_index("remediation_assessments_submission_idx", table_name="remediation_assessments")
    op.drop_index("remediation_assessments_task_idx", table_name="remediation_assessments")
    op.drop_table("remediation_assessments")
    op.drop_constraint(
        "remediation_evidence_parse_status_ck", "remediation_evidence", type_="check"
    )
    op.drop_column("remediation_evidence", "parse_status")
    op.drop_column("remediation_evidence", "parsed_text")
    op.drop_column("remediation_tasks", "source_issue_id")
    op.drop_column("remediation_tasks", "source_review_result_id")
