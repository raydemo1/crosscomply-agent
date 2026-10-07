"""Keep case feedback separate for each participant."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0016_case_feedback_by_actor"
down_revision: str | None = "0015_default_remediation_phase"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("case_feedback_pkey", "case_feedback", type_="primary")
    op.create_primary_key("case_feedback_pkey", "case_feedback", ["case_id", "actor_id"])


def downgrade() -> None:
    op.execute("DELETE FROM case_feedback a USING case_feedback b WHERE a.case_id = b.case_id AND (a.updated_at, a.actor_id) < (b.updated_at, b.actor_id)")
    op.drop_constraint("case_feedback_pkey", "case_feedback", type_="primary")
    op.create_primary_key("case_feedback_pkey", "case_feedback", ["case_id"])
