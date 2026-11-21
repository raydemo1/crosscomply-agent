"""Reranking is a deployment setting (RERANK_MODE), not a per-case or per-template choice."""

from alembic import op

revision = "0024_drop_case_rerank_mode"
down_revision = "0023_eval_case_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE review_cases DROP COLUMN rerank_mode")
    op.execute("ALTER TABLE case_templates DROP COLUMN rerank_mode")


def downgrade() -> None:
    op.execute("ALTER TABLE review_cases ADD COLUMN rerank_mode text NOT NULL DEFAULT 'off'")
    op.execute("ALTER TABLE case_templates ADD COLUMN rerank_mode text NOT NULL DEFAULT 'off'"
               " CONSTRAINT case_templates_rerank_mode_ck CHECK (rerank_mode IN ('off', 'embedding'))")
