"""Group durable import batches into bounded full-history manifests."""

from alembic import op


revision = "0015_import_histories"
down_revision = "0014_recording_groups"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE import_histories (
        id BIGSERIAL PRIMARY KEY,
        account_id VARCHAR(255) NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        request_id UUID NOT NULL,
        manifest_hash CHAR(64) NOT NULL CHECK (manifest_hash ~ '^[0-9a-f]{64}$'),
        label VARCHAR(200) NOT NULL CHECK (length(label) BETWEEN 1 AND 200),
        state VARCHAR(16) NOT NULL DEFAULT 'open' CHECK (state IN ('open','stopped')),
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        UNIQUE (account_id,request_id),
        UNIQUE (account_id,id)
    )""")
    op.execute("CREATE INDEX ix_import_histories_owner_created ON import_histories(account_id,created_at DESC,id DESC)")
    op.execute("""ALTER TABLE import_batches
        ADD COLUMN history_id BIGINT,
        ADD COLUMN history_ordinal SMALLINT,
        ADD CONSTRAINT fk_import_batches_history_owner
            FOREIGN KEY (account_id,history_id) REFERENCES import_histories(account_id,id) ON DELETE CASCADE,
        ADD CONSTRAINT ck_import_batches_history_ordinal CHECK (
            (history_id IS NULL AND history_ordinal IS NULL) OR
            (history_id IS NOT NULL AND history_ordinal IS NOT NULL AND history_ordinal BETWEEN 0 AND 49)
        )""")
    op.execute("CREATE UNIQUE INDEX uq_import_batches_history_ordinal ON import_batches(account_id,history_id,history_ordinal) WHERE history_id IS NOT NULL")
    op.execute("CREATE INDEX ix_import_batches_history_page ON import_batches(account_id,history_id,history_ordinal) WHERE history_id IS NOT NULL")
    op.execute("""ALTER TABLE import_batch_items
        ADD COLUMN expected_sha256 CHAR(64),
        ADD COLUMN expected_size BIGINT,
        ADD CONSTRAINT ck_import_batch_item_expected_identity CHECK (
            (expected_sha256 IS NULL AND expected_size IS NULL) OR
            (expected_sha256 IS NOT NULL AND expected_size IS NOT NULL AND
             expected_sha256 ~ '^[0-9a-f]{64}$' AND expected_size BETWEEN 1 AND 10485760)
        )""")
    op.execute("""CREATE FUNCTION collect_sync_revision_import_history() RETURNS trigger AS $$
        BEGIN
            IF TG_OP='UPDATE' AND ROW(OLD.label,OLD.state,OLD.manifest_hash)
                IS NOT DISTINCT FROM ROW(NEW.label,NEW.state,NEW.manifest_hash) THEN RETURN NULL; END IF;
            IF TG_OP='DELETE' THEN PERFORM queue_sync_revision_owner(OLD.account_id);
            ELSE PERFORM queue_sync_revision_owner(NEW.account_id); END IF;
            RETURN NULL;
        END
        $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER trg_import_histories_sync_revision_collect
        AFTER INSERT OR UPDATE OR DELETE ON import_histories
        FOR EACH ROW EXECUTE FUNCTION collect_sync_revision_import_history()""")
    op.execute("""CREATE CONSTRAINT TRIGGER trg_import_histories_sync_revision_flush
        AFTER INSERT OR UPDATE OR DELETE ON import_histories
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
        EXECUTE FUNCTION flush_sync_revision_owners()""")


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS trg_import_histories_sync_revision_flush ON import_histories")
    op.execute("DROP TRIGGER IF EXISTS trg_import_histories_sync_revision_collect ON import_histories")
    op.execute("DROP FUNCTION IF EXISTS collect_sync_revision_import_history()")
    op.execute("ALTER TABLE import_batch_items DROP CONSTRAINT IF EXISTS ck_import_batch_item_expected_identity")
    op.execute("ALTER TABLE import_batch_items DROP COLUMN IF EXISTS expected_sha256,DROP COLUMN IF EXISTS expected_size")
    op.execute("DROP INDEX IF EXISTS ix_import_batches_history_page")
    op.execute("DROP INDEX IF EXISTS uq_import_batches_history_ordinal")
    op.execute("ALTER TABLE import_batches DROP CONSTRAINT IF EXISTS ck_import_batches_history_ordinal")
    op.execute("ALTER TABLE import_batches DROP CONSTRAINT IF EXISTS fk_import_batches_history_owner")
    op.execute("ALTER TABLE import_batches DROP COLUMN IF EXISTS history_ordinal,DROP COLUMN IF EXISTS history_id")
    op.execute("DROP TABLE IF EXISTS import_histories")
