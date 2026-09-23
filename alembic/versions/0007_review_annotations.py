"""Persist annotations anchored to frozen review materials."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007_review_annotations"
down_revision: str | None = "0006_revision_accepted_text"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "review_annotations",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("case_id", sa.Text(), sa.ForeignKey("review_cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("review_result_id", sa.Text(), nullable=False),
        sa.Column("material_version_id", sa.Text(), sa.ForeignKey("material_versions.id"), nullable=False),
        sa.Column("start_offset", sa.Integer(), nullable=False),
        sa.Column("end_offset", sa.Integer(), nullable=False),
        sa.Column("quote", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False, server_default=""),
        sa.Column("finding", sa.Text(), nullable=False),
        sa.Column("recommendation", sa.Text(), nullable=False, server_default=""),
        sa.Column("citation_refs", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("insufficient_evidence", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("decided_by", sa.Text(), sa.ForeignKey("users.id")),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("end_offset > start_offset", name="review_annotations_range_ck"),
        sa.CheckConstraint("source IN ('human', 'model')", name="review_annotations_source_ck"),
        sa.CheckConstraint("status IN ('pending', 'confirmed', 'rejected')", name="review_annotations_status_ck"),
    )
    op.create_index("review_annotations_case_idx", "review_annotations", ["case_id", "created_at"])


def downgrade() -> None:
    op.drop_index("review_annotations_case_idx", table_name="review_annotations")
    op.drop_table("review_annotations")
