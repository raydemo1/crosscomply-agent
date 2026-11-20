"""Persist yearly entity portfolio snapshots and acknowledgements."""

from alembic import op

revision = "0021_entity_portfolio"
down_revision = "0020_transfer_work_inbox"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""ALTER TABLE transfer_activities
        ADD COLUMN entity_key text NOT NULL DEFAULT '',
        ADD COLUMN counts_toward_total boolean NOT NULL DEFAULT true,
        ADD COLUMN exclusion_note text NOT NULL DEFAULT ''""")
    op.execute("ALTER TABLE transfer_activity_reviews ADD COLUMN entity_key text NOT NULL DEFAULT ''")
    for table in ("transfer_activities", "transfer_activity_reviews"):
        op.execute("UPDATE " + table + r" SET entity_key = lower(regexp_replace(business_entity, '\s', '', 'g'))")
    op.execute("CREATE INDEX transfer_activities_entity_idx ON transfer_activities (entity_key)")
    op.execute("""CREATE TABLE portfolio_snapshots (
        id text PRIMARY KEY,
        case_id text NOT NULL REFERENCES review_cases(id),
        entity_key text NOT NULL,
        year integer NOT NULL,
        excluded_activity_id text,
        payload jsonb NOT NULL,
        fingerprint text NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        UNIQUE (case_id, fingerprint),
        UNIQUE (case_id, id)
    )""")
    op.execute("""ALTER TABLE review_tasks ADD COLUMN portfolio_snapshot_id text,
        ADD CONSTRAINT review_tasks_portfolio_fk FOREIGN KEY (case_id, portfolio_snapshot_id)
        REFERENCES portfolio_snapshots(case_id, id)""")
    op.execute("""CREATE TABLE portfolio_alert_acks (
        entity_key text NOT NULL,
        year integer NOT NULL,
        band text NOT NULL,
        acked_by text NOT NULL REFERENCES users(id),
        note text NOT NULL,
        acked_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (entity_key, year, band)
    )""")


def downgrade() -> None:
    op.execute("DROP TABLE portfolio_alert_acks")
    op.execute("ALTER TABLE review_tasks DROP COLUMN portfolio_snapshot_id")
    op.execute("DROP TABLE portfolio_snapshots")
    op.execute("DROP INDEX transfer_activities_entity_idx")
    op.execute("ALTER TABLE transfer_activity_reviews DROP COLUMN entity_key")
    op.execute("""ALTER TABLE transfer_activities DROP COLUMN entity_key,
        DROP COLUMN counts_toward_total, DROP COLUMN exclusion_note""")
