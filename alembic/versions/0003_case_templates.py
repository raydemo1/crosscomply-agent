"""Create reusable new-case templates."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_case_templates"
down_revision: str | None = "0002_remediation_plan"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "case_templates",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column(
            "intake_json",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("review_mode", sa.Text(), server_default="llm", nullable=False),
        sa.Column("rerank_mode", sa.Text(), server_default="off", nullable=False),
        sa.Column("created_by", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("updated_by", sa.Text(), sa.ForeignKey("users.id")),
        sa.Column("archived", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("review_mode IN ('llm', 'multi_agent')", name="case_templates_review_mode_ck"),
        sa.CheckConstraint("rerank_mode IN ('off', 'embedding')", name="case_templates_rerank_mode_ck"),
    )
    op.create_index("case_templates_active_idx", "case_templates", ["archived", "updated_at"])


def downgrade() -> None:
    op.drop_index("case_templates_active_idx", table_name="case_templates")
    op.drop_table("case_templates")
