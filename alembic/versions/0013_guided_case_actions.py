"""Add guided applicant actions and agent verified submissions."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013_guided_case_actions"
down_revision: str | None = "0012_agent_facts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "remediation_tasks",
        sa.Column("task_kind", sa.Text(), nullable=False, server_default="control_remediation"),
    )
    op.add_column(
        "remediation_tasks",
        sa.Column("phase", sa.Text(), nullable=False, server_default="post_approval"),
    )
    op.add_column(
        "remediation_tasks",
        sa.Column("blocking", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "remediation_tasks",
        sa.Column("source_key", sa.Text(), nullable=True),
    )
    op.add_column(
        "remediation_tasks",
        sa.Column("is_current", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_check_constraint(
        "remediation_tasks_kind_ck",
        "remediation_tasks",
        "task_kind IN ('fact_confirmation', 'control_remediation', 'recommendation')",
    )
    op.create_check_constraint(
        "remediation_tasks_phase_ck",
        "remediation_tasks",
        "phase IN ('pre_approval', 'post_approval')",
    )
    op.create_index(
        "remediation_tasks_source_key_uq",
        "remediation_tasks",
        ["plan_id", "source_key"],
        unique=True,
    )

    op.add_column(
        "remediation_evidence",
        sa.Column(
            "material_version_id",
            sa.Text(),
            sa.ForeignKey("material_versions.id"),
            nullable=True,
        ),
    )

    op.drop_constraint(
        "remediation_submissions_status_ck",
        "remediation_submissions",
        type_="check",
    )
    op.create_check_constraint(
        "remediation_submissions_status_ck",
        "remediation_submissions",
        "status IN ('pending_review', 'accepted', 'rejected', 'agent_verified', 'agent_feedback')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "remediation_submissions_status_ck",
        "remediation_submissions",
        type_="check",
    )
    op.create_check_constraint(
        "remediation_submissions_status_ck",
        "remediation_submissions",
        "status IN ('pending_review', 'accepted', 'rejected')",
    )
    op.drop_column("remediation_evidence", "material_version_id")
    op.drop_index("remediation_tasks_source_key_uq", table_name="remediation_tasks")
    op.drop_constraint("remediation_tasks_phase_ck", "remediation_tasks", type_="check")
    op.drop_constraint("remediation_tasks_kind_ck", "remediation_tasks", type_="check")
    for column in ("is_current", "source_key", "blocking", "phase", "task_kind"):
        op.drop_column("remediation_tasks", column)
