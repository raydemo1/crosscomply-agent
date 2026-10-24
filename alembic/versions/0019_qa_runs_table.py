"""QA runs split into their own table with indexed claimable status.

The single-JSONB qa_conversations.data row forced worker claim polling to run
jsonb_array_elements over every conversation. qa_runs now carries each run as
a row; claim becomes an indexed status lookup. Existing JSONB runs are
migrated into the new table; the JSONB copy stays as read-through fallback
until the next release removes it.
"""

from alembic import op

revision = "0019_qa_runs_table"
down_revision = "0018_qa_conversations"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE qa_runs (
        id text PRIMARY KEY,
        conversation_id text NOT NULL REFERENCES qa_conversations(id) ON DELETE CASCADE,
        request_id text NOT NULL,
        question text NOT NULL DEFAULT '',
        material_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
        status text NOT NULL DEFAULT 'queued',
        attempt int NOT NULL DEFAULT 0,
        state jsonb NOT NULL DEFAULT '{}'::jsonb,
        answer text NOT NULL DEFAULT '',
        result jsonb,
        error text,
        progress text NOT NULL DEFAULT '',
        lease_expires_at timestamptz,
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (conversation_id, request_id)
    )""")
    op.execute(
        "CREATE INDEX qa_runs_claimable ON qa_runs (status, created_at) "
        "WHERE status IN ('queued', 'running')"
    )
    op.execute(
        "CREATE INDEX qa_runs_conversation ON qa_runs (conversation_id, created_at)"
    )
    # 存量 JSONB runs 展开迁入新表；失败不阻塞（新表为空也能正常工作）
    op.execute("""
        INSERT INTO qa_runs (
            id, conversation_id, request_id, question, material_ids, status,
            attempt, state, answer, result, error, progress, lease_expires_at, created_at
        )
        SELECT
            r->>'id',
            c.id,
            r->>'request_id',
            COALESCE(r->>'question', ''),
            COALESCE(r->'material_ids', '[]'::jsonb),
            COALESCE(r->>'status', 'queued'),
            COALESCE((r->>'attempt')::int, 0),
            COALESCE(r->'state', '{}'::jsonb),
            COALESCE(r->>'answer', ''),
            r->'result',
            r->>'error',
            COALESCE(r->>'progress', ''),
            (r->>'lease_expires_at')::timestamptz,
            COALESCE((r->>'created_at')::timestamptz, now())
        FROM qa_conversations c
        CROSS JOIN LATERAL jsonb_array_elements(c.data->'runs') AS r
        ON CONFLICT (id) DO NOTHING
    """)


def downgrade():
    op.execute("DROP TABLE IF EXISTS qa_runs")
