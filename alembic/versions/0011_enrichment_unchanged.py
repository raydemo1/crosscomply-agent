"""Record official sources whose downloaded body already exists in the KB.

Revision ID: 0011_enrichment_unchanged
Revises: 0010_knowledge_enrichment
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0011_enrichment_unchanged"
down_revision: str | None = "0010_knowledge_enrichment"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("knowledge_enrichment_jobs_status_ck", "knowledge_enrichment_jobs", type_="check")
    op.create_check_constraint(
        "knowledge_enrichment_jobs_status_ck", "knowledge_enrichment_jobs",
        "status IN ('queued', 'running', 'awaiting_review', 'approved', "
        "'published', 'unchanged', 'rejected', 'failed')",
    )


def downgrade() -> None:
    op.execute("UPDATE knowledge_enrichment_jobs SET status='rejected' WHERE status='unchanged'")
    op.drop_constraint("knowledge_enrichment_jobs_status_ck", "knowledge_enrichment_jobs", type_="check")
    op.create_check_constraint(
        "knowledge_enrichment_jobs_status_ck", "knowledge_enrichment_jobs",
        "status IN ('queued', 'running', 'awaiting_review', 'approved', "
        "'published', 'rejected', 'failed')",
    )
