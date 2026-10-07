"""Persist the answer type specified for each case action."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_remediation_answer_type"
down_revision: str | None = "0016_case_feedback_by_actor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("remediation_tasks", sa.Column("answer_type", sa.Text(), server_default="control_status", nullable=False))
    op.execute("UPDATE remediation_tasks SET answer_type = 'choice' WHERE task_kind = 'fact_confirmation'")
    op.create_check_constraint("remediation_tasks_answer_type_ck", "remediation_tasks", "answer_type IN ('choice', 'count', 'text', 'control_status')")


def downgrade() -> None:
    op.drop_constraint("remediation_tasks_answer_type_ck", "remediation_tasks", type_="check")
    op.drop_column("remediation_tasks", "answer_type")
