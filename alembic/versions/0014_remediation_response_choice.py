"""Record applicants' structured remediation responses."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0014_remediation_response_choice"
down_revision: str | None = "0013_guided_case_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("remediation_submissions", sa.Column("response_choice", sa.Text(), nullable=True))
    op.create_check_constraint(
        "remediation_submissions_response_choice_ck",
        "remediation_submissions",
        "response_choice IS NULL OR response_choice IN ('yes', 'no', 'unknown', 'completed', 'incomplete', 'not_applicable')",
    )


def downgrade() -> None:
    op.drop_constraint("remediation_submissions_response_choice_ck", "remediation_submissions", type_="check")
    op.drop_column("remediation_submissions", "response_choice")
