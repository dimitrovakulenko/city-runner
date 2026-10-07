"""Independent account deletion receipts and original-file cleanup outbox."""

from alembic import op


revision = "0011_account_data"
down_revision = "0010_routes"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE account_deletions (
            id UUID PRIMARY KEY,
            account_id VARCHAR(255) NOT NULL UNIQUE,
            requested_at TIMESTAMPTZ NOT NULL,
            status VARCHAR(20) NOT NULL DEFAULT 'cleanup-pending'
                CHECK (status IN ('cleanup-pending','complete')),
            completed_at TIMESTAMPTZ
        )
    """)
    op.execute("""
        CREATE TABLE account_deletion_outbox (
            id BIGSERIAL PRIMARY KEY,
            deletion_id UUID NOT NULL REFERENCES account_deletions(id) ON DELETE CASCADE,
            account_id VARCHAR(255) NOT NULL,
            object_key VARCHAR(40) NOT NULL CHECK (object_key ~ '^[0-9a-f]{32}\\.(gpx|fit)$'),
            status VARCHAR(16) NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','running','failed','complete')),
            attempts SMALLINT NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 100),
            max_attempts SMALLINT NOT NULL DEFAULT 8 CHECK (max_attempts BETWEEN 1 AND 100),
            available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            lease_token UUID,
            leased_until TIMESTAMPTZ,
            last_error VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (deletion_id,object_key),
            CHECK ((status='running' AND lease_token IS NOT NULL AND leased_until IS NOT NULL)
                OR (status<>'running' AND lease_token IS NULL AND leased_until IS NULL))
        )
    """)
    op.execute("CREATE INDEX ix_account_deletion_outbox_claim ON account_deletion_outbox(status,available_at,id)")
    op.execute("CREATE INDEX ix_account_deletion_outbox_receipt ON account_deletion_outbox(deletion_id,status)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_account_deletion_outbox_receipt")
    op.execute("DROP INDEX IF EXISTS ix_account_deletion_outbox_claim")
    op.execute("DROP TABLE IF EXISTS account_deletion_outbox")
    op.execute("DROP TABLE IF EXISTS account_deletions")
