"""Persist citation dependencies and regulation-change follow-up."""

from alembic import op

revision = "0022_regulation_impact"
down_revision = "0021_entity_portfolio"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE case_citation_index (
        task_id text NOT NULL REFERENCES review_tasks(id),
        case_id text NOT NULL REFERENCES review_cases(id),
        chunk_id text NOT NULL,
        source_id text NOT NULL,
        instrument_key text NOT NULL DEFAULT '',
        article_no text,
        title text NOT NULL,
        effective_date text,
        can_cite_clause boolean NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (task_id, chunk_id)
    )""")
    op.execute("CREATE INDEX case_citation_index_source_idx ON case_citation_index (source_id)")
    op.execute("CREATE INDEX case_citation_index_instrument_idx ON case_citation_index (instrument_key)")
    op.execute("""CREATE TABLE knowledge_change_events (
        id text PRIMARY KEY,
        job_id text,
        source_id text NOT NULL,
        instrument_key text NOT NULL DEFAULT '',
        change_type text NOT NULL,
        detail jsonb NOT NULL,
        impact_status text NOT NULL DEFAULT 'pending',
        impact_error text,
        created_by text,
        created_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("""CREATE TABLE case_impact_flags (
        id text PRIMARY KEY,
        change_event_id text NOT NULL REFERENCES knowledge_change_events(id),
        case_id text NOT NULL REFERENCES review_cases(id),
        task_id text NOT NULL REFERENCES review_tasks(id),
        activity_id text REFERENCES transfer_activities(id),
        reason text NOT NULL,
        severity text NOT NULL,
        chunk_ids jsonb NOT NULL,
        article_nos jsonb NOT NULL,
        effective_on date,
        status text NOT NULL DEFAULT 'open',
        resolution_note text,
        resolved_by text REFERENCES users(id),
        resolved_at timestamptz,
        rereview_case_id text REFERENCES review_cases(id),
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (change_event_id, case_id)
    )""")
    op.execute("CREATE INDEX case_impact_flags_open_idx ON case_impact_flags (status, case_id, task_id)")
    op.execute("CREATE INDEX case_impact_flags_rereview_idx ON case_impact_flags (rereview_case_id) WHERE rereview_case_id IS NOT NULL")


def downgrade() -> None:
    op.execute("DROP TABLE case_impact_flags")
    op.execute("DROP TABLE knowledge_change_events")
    op.execute("DROP TABLE case_citation_index")
