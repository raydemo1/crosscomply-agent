"""Persist reviewer-owned, deidentified field evaluation drafts."""

from alembic import op

revision = "0023_eval_case_drafts"
down_revision = "0022_regulation_impact"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE eval_case_drafts (
        id text PRIMARY KEY,
        case_id text NOT NULL REFERENCES review_cases(id),
        task_id text NOT NULL REFERENCES review_tasks(id),
        signals jsonb NOT NULL,
        original_entities jsonb NOT NULL,
        status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'ready', 'exported', 'discarded')),
        question text NOT NULL,
        material_text text NOT NULL,
        intake jsonb NOT NULL,
        agent_output jsonb NOT NULL,
        reviewer_correction text NOT NULL DEFAULT '',
        rubric jsonb NOT NULL DEFAULT '{}',
        selection_reason text NOT NULL DEFAULT '',
        reference_basis jsonb NOT NULL DEFAULT '[]',
        deidentified boolean NOT NULL DEFAULT false,
        created_by text NOT NULL REFERENCES users(id),
        updated_by text NOT NULL REFERENCES users(id),
        created_at timestamptz NOT NULL DEFAULT now(),
        updated_at timestamptz NOT NULL DEFAULT now(),
        exported_case_id text,
        exported_sha256 text
    )""")
    op.execute("CREATE UNIQUE INDEX eval_case_drafts_task_idx ON eval_case_drafts (task_id) WHERE status <> 'discarded'")


def downgrade() -> None:
    op.execute("DROP TABLE eval_case_drafts")
