"""Durable manifests for bounded GPX/FIT batch uploads."""
from alembic import op

revision = "0009_import_batches"
down_revision = "0008_corrections"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE import_batches (
            id BIGSERIAL PRIMARY KEY,
            account_id VARCHAR(255) NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            request_id UUID NOT NULL,
            manifest_hash CHAR(64) NOT NULL,
            state VARCHAR(16) NOT NULL DEFAULT 'open' CHECK (state IN ('open','stopped')),
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (account_id, request_id),
            UNIQUE (account_id, id)
        )
    """)
    op.execute("""
        CREATE TABLE import_batch_items (
            id BIGSERIAL PRIMARY KEY,
            account_id VARCHAR(255) NOT NULL,
            batch_id BIGINT NOT NULL,
            ordinal SMALLINT NOT NULL CHECK (ordinal BETWEEN 0 AND 19),
            name VARCHAR(200) NOT NULL,
            format VARCHAR(3) NOT NULL CHECK (format IN ('gpx','fit')),
            content_hash CHAR(64),
            source_id BIGINT,
            duplicate BOOLEAN NOT NULL DEFAULT false,
            status VARCHAR(20) NOT NULL DEFAULT 'awaiting_upload'
                CHECK (status IN ('awaiting_upload','queued','processing','succeeded','failed','deleted')),
            error_code VARCHAR(64),
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            FOREIGN KEY (account_id,batch_id) REFERENCES import_batches(account_id,id) ON DELETE CASCADE,
            UNIQUE (batch_id,ordinal),
            UNIQUE (account_id,batch_id,id)
        )
    """)
    op.execute("CREATE INDEX ix_import_batches_owner_created ON import_batches(account_id,created_at DESC,id DESC)")
    op.execute("CREATE INDEX ix_import_batch_items_source ON import_batch_items(account_id,source_id)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS import_batch_items")
    op.execute("DROP TABLE IF EXISTS import_batches")
