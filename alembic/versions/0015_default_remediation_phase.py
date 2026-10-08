"""Default manually created remediation tasks to the current review cycle."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0015_default_remediation_phase"
down_revision: str | None = "0014_remediation_response_choice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "remediation_tasks",
        "phase",
        existing_type=sa.Text(),
        server_default="pre_approval",
    )


def downgrade() -> None:
    op.alter_column(
        "remediation_tasks",
        "phase",
        existing_type=sa.Text(),
        server_default="post_approval",
    )
