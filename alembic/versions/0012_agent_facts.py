"""Replace rule determinations with frozen applicant intake.

Revision ID: 0012_agent_facts
Revises: 0011_enrichment_unchanged
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0012_agent_facts"
down_revision: str | None = "0011_enrichment_unchanged"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    conn = op.get_bind()
    approvals = conn.execute(sa.text("SELECT count(*) FROM approval_records")).scalar_one()
    reports = conn.execute(sa.text("SELECT count(*) FROM report_records")).scalar_one()
    if approvals or reports:
        raise RuntimeError("存在飞书审批或正式报告，请先处理后再切换审查主链")

    conn.execute(sa.text("DELETE FROM approval_delivery_jobs"))
    conn.execute(sa.text("DELETE FROM review_tasks"))
    conn.execute(sa.text("DELETE FROM rule_snapshots"))
    conn.execute(sa.text(
        "UPDATE review_cases SET status='draft', facts_confirmed=false, "
        "risk_level=NULL, trace_id=NULL, response_json=NULL, updated_at=now() "
        "WHERE status NOT IN ('approved', 'conditionally_approved', 'rejected')"
    ))

    op.rename_table("rule_snapshots", "intake_snapshots")
    op.alter_column("review_tasks", "rule_snapshot_id", new_column_name="intake_snapshot_id")
    op.drop_column("intake_snapshots", "ruleset_version")
    op.drop_column("intake_snapshots", "determination_json")
    op.alter_column("intake_snapshots", "facts_json", new_column_name="intake_json")
    op.add_column("intake_snapshots", sa.Column("fingerprint", sa.String(length=64), nullable=False))
    op.add_column(
        "intake_snapshots",
        sa.Column("created_by", sa.Text(), sa.ForeignKey("users.id"), nullable=False),
    )
    op.create_unique_constraint(
        "intake_snapshots_case_fingerprint_uq", "intake_snapshots", ["case_id", "fingerprint"]
    )


def downgrade() -> None:
    raise RuntimeError("此迁移清理了旧审查任务，不能自动回退")
