"""Keep the inferred source draft and the page date a discovered source was found with.

A job awaiting review carries the metadata the worker inferred from the
document, the evidence for each field and the editions it would supersede, so
the reviewer confirms a prefilled draft. ``published_date`` is the date the
search engine reported for the page, one more piece of date evidence.

Revision ID: 0025_enrichment_draft
Revises: 0024_drop_case_rerank_mode
"""

from alembic import op

revision = "0025_enrichment_draft"
down_revision = "0024_drop_case_rerank_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE knowledge_enrichment_jobs ADD COLUMN draft_json jsonb")
    op.execute("ALTER TABLE knowledge_enrichment_jobs ADD COLUMN published_date text")


def downgrade() -> None:
    op.execute("ALTER TABLE knowledge_enrichment_jobs DROP COLUMN published_date")
    op.execute("ALTER TABLE knowledge_enrichment_jobs DROP COLUMN draft_json")
