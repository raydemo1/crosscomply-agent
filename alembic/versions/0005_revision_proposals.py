"""Persist human-reviewed text revision proposals."""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005_revision_proposals"
down_revision: str | None = "0004_agent_runtime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "revision_proposals",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("case_id", sa.Text(), sa.ForeignKey("review_cases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_review_result_id", sa.Text(), nullable=False),
        sa.Column("issue_id", sa.Text(), nullable=False),
        sa.Column("source_material_version_id", sa.Text(), sa.ForeignKey("material_versions.id"), nullable=False),
        sa.Column("target_quote", sa.Text(), nullable=False),
        sa.Column("target_start", sa.Integer(), nullable=False),
        sa.Column("target_end", sa.Integer(), nullable=False),
        sa.Column("base_version", sa.Integer(), nullable=False),
        sa.Column("base_sha256", sa.Text(), nullable=False),
        sa.Column("proposed_text", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("open_points_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("citation_refs_json", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("status", sa.Text(), nullable=False, server_default="pending"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("result_text", sa.Text()),
        sa.Column("result_sha256", sa.Text()),
        sa.Column("result_version", sa.Integer()),
        sa.Column("decision_note", sa.Text()),
        sa.Column("created_by", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("decided_by", sa.Text(), sa.ForeignKey("users.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('pending', 'accepted', 'rejected', 'superseded')", name="revision_proposals_status_ck"),
    )
    op.create_index("revision_proposals_case_created_idx", "revision_proposals", ["case_id", "created_at"])
    op.create_index("revision_proposals_material_version_idx", "revision_proposals", ["source_material_version_id", "result_version"])


def downgrade() -> None:
    op.drop_index("revision_proposals_material_version_idx", table_name="revision_proposals")
    op.drop_index("revision_proposals_case_created_idx", table_name="revision_proposals")
    op.drop_table("revision_proposals")
