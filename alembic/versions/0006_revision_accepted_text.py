"""Preserve the exact human-approved replacement text."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006_revision_accepted_text"
down_revision: str | None = "0005_revision_proposals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("revision_proposals", sa.Column("accepted_text", sa.Text()))
    op.create_index(
        "revision_proposals_one_pending_target_idx", "revision_proposals",
        ["case_id", "source_review_result_id", "issue_id", "source_material_version_id", "target_start", "target_end", "base_sha256"],
        unique=True, postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("revision_proposals_one_pending_target_idx", table_name="revision_proposals")
    op.drop_column("revision_proposals", "accepted_text")
