"""Persist transfer activities, their reviews, and work reminder deliveries."""

from alembic import op

revision = "0020_transfer_work_inbox"
down_revision = "0019_qa_runs_table"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE transfer_activities (
        id text PRIMARY KEY,
        origin_case_id text NOT NULL UNIQUE REFERENCES review_cases(id),
        owner_id text NOT NULL REFERENCES users(id),
        title text NOT NULL,
        business_entity text NOT NULL,
        next_review_on date NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX transfer_activities_owner_idx ON transfer_activities (owner_id, next_review_on)")
    op.execute("""CREATE TABLE transfer_activity_reviews (
        case_id text PRIMARY KEY REFERENCES review_cases(id),
        activity_id text NOT NULL REFERENCES transfer_activities(id),
        baseline_case_id text NOT NULL REFERENCES review_cases(id),
        business_entity text NOT NULL,
        change_note text NOT NULL,
        impact jsonb,
        created_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("CREATE INDEX transfer_activity_reviews_activity_idx ON transfer_activity_reviews (activity_id, created_at)")
    op.execute("""CREATE TABLE work_reminder_deliveries (
        id uuid PRIMARY KEY,
        retry_after timestamptz NOT NULL,
        sent_at timestamptz
    )""")


def downgrade() -> None:
    op.execute("DROP TABLE work_reminder_deliveries")
    op.execute("DROP TABLE transfer_activity_reviews")
    op.execute("DROP TABLE transfer_activities")
