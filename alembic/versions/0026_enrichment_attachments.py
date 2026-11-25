"""Discovered notices whose instrument is an attached file are followed to the attachments.

An official page is often only the notice that issues a rule ("现将《……办法》印发给
你们") with the rule itself attached as a PDF. Publishing the notice would index a
shell; the worker instead marks the job ``expanded`` and queues one child job per
attachment. ``parent_job_id`` links a child to its notice and ``context_text`` keeps
the notice's text, which names the issuing body, the document number and the date
the attachment itself often omits.

Revision ID: 0026_enrichment_attachments
Revises: 0025_enrichment_draft
"""

from alembic import op

revision = "0026_enrichment_attachments"
down_revision = "0025_enrichment_draft"
branch_labels = None
depends_on = None

STATUSES = ("'queued', 'running', 'awaiting_review', 'approved', 'published', 'unchanged', 'rejected', 'failed'")


def upgrade() -> None:
    op.execute("ALTER TABLE knowledge_enrichment_jobs ADD COLUMN parent_job_id text "
               "REFERENCES knowledge_enrichment_jobs(id) ON DELETE CASCADE")
    op.execute("ALTER TABLE knowledge_enrichment_jobs ADD COLUMN context_text text")
    op.drop_constraint("knowledge_enrichment_jobs_status_ck", "knowledge_enrichment_jobs", type_="check")
    op.create_check_constraint("knowledge_enrichment_jobs_status_ck", "knowledge_enrichment_jobs",
                               f"status IN ({STATUSES}, 'expanded')")


def downgrade() -> None:
    op.execute("UPDATE knowledge_enrichment_jobs SET status='rejected' WHERE status='expanded'")
    op.drop_constraint("knowledge_enrichment_jobs_status_ck", "knowledge_enrichment_jobs", type_="check")
    op.create_check_constraint("knowledge_enrichment_jobs_status_ck", "knowledge_enrichment_jobs",
                               f"status IN ({STATUSES})")
    op.execute("ALTER TABLE knowledge_enrichment_jobs DROP COLUMN context_text")
    op.execute("ALTER TABLE knowledge_enrichment_jobs DROP COLUMN parent_job_id")
