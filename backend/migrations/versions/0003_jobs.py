"""Add account-scoped durable jobs."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_jobs"
down_revision = "0002_accounts"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "jobs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("account_id", sa.String(255), sa.ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(100), nullable=False),
        sa.Column("dedupe_key", sa.String(255), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.SmallInteger(), nullable=False, server_default="5"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("lease_token", sa.String(32)),
        sa.Column("leased_until", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('queued','running','succeeded','failed','cancelled')", name="ck_jobs_status"),
        sa.CheckConstraint("attempts >= 0 AND max_attempts > 0", name="ck_jobs_attempts"),
        sa.CheckConstraint("(status = 'running' AND lease_token IS NOT NULL AND leased_until IS NOT NULL) OR (status <> 'running' AND lease_token IS NULL AND leased_until IS NULL)", name="ck_jobs_lease"),
        sa.UniqueConstraint("account_id", "kind", "dedupe_key", name="uq_jobs_account_kind_dedupe"),
    )
    op.create_index("ix_jobs_claim", "jobs", ["status", "available_at", "priority", "id"])
    op.create_index("ix_jobs_running_lease", "jobs", ["leased_until", "id"], postgresql_where=sa.text("status='running'"))
    op.create_index("ix_jobs_account_status", "jobs", ["account_id", "status", "created_at"])


def downgrade():
    op.drop_index("ix_jobs_account_status", table_name="jobs")
    op.drop_index("ix_jobs_running_lease", table_name="jobs")
    op.drop_index("ix_jobs_claim", table_name="jobs")
    op.drop_table("jobs")
