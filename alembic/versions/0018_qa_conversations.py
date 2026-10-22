"""Private compliance Q&A and independent investigation runs."""

from alembic import op

revision = "0018_qa_conversations"
down_revision = "0017_remediation_answer_type"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE qa_conversations (
        id text PRIMARY KEY,
        owner_id text NOT NULL REFERENCES users(id),
        data jsonb NOT NULL,
        updated_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX qa_conversations_owner_updated ON qa_conversations(owner_id, updated_at DESC)")


def downgrade():
    op.drop_table("qa_conversations")
