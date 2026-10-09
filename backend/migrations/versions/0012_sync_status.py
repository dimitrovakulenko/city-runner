"""Transactional owner/geography change revisions for sync polling."""

from alembic import op


revision = "0012_sync_status"
down_revision = "0011_account_data"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE sync_account_revisions (
        account_id VARCHAR(255) PRIMARY KEY,
        revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0)
    )""")
    op.execute("""CREATE TABLE sync_geography_revision (
        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
        revision BIGINT NOT NULL DEFAULT 0 CHECK (revision >= 0)
    )""")
    op.execute("INSERT INTO sync_geography_revision(singleton) VALUES (TRUE)")
    op.execute("""CREATE TABLE sync_revision_events (
        transaction_id BIGINT NOT NULL,
        account_id VARCHAR(255) NOT NULL,
        PRIMARY KEY (transaction_id, account_id)
    )""")

    op.execute("""
        CREATE FUNCTION queue_sync_revision_owner(owner_id VARCHAR(255)) RETURNS void AS $$
        BEGIN
            IF owner_id IS NULL THEN RETURN; END IF;
            INSERT INTO sync_revision_events(transaction_id,account_id)
                VALUES (txid_current(),owner_id) ON CONFLICT DO NOTHING;
            PERFORM set_config('city_runner.sync_revision_flushed','0',TRUE);
        END
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE FUNCTION flush_sync_revision_owners() RETURNS trigger AS $$
        DECLARE owner_row RECORD;
        BEGIN
            IF current_setting('city_runner.sync_revision_flushed',TRUE)='1' THEN
                RETURN NULL;
            END IF;
            PERFORM set_config('city_runner.sync_revision_flushed','1',TRUE);
            FOR owner_row IN
                SELECT account_id FROM sync_revision_events
                WHERE transaction_id=txid_current() ORDER BY account_id
            LOOP
                IF EXISTS (SELECT 1 FROM accounts WHERE id=owner_row.account_id) THEN
                    INSERT INTO sync_account_revisions(account_id,revision)
                    VALUES (owner_row.account_id,1)
                    ON CONFLICT (account_id) DO UPDATE
                      SET revision=sync_account_revisions.revision+1;
                ELSE
                    DELETE FROM sync_account_revisions WHERE account_id=owner_row.account_id;
                END IF;
            END LOOP;
            DELETE FROM sync_revision_events WHERE transaction_id=txid_current();
            RETURN NULL;
        END
        $$ LANGUAGE plpgsql
    """)

    # Immediate row triggers collect the complete owner set. The first deferred
    # trigger flushes it in owner-ID order at commit, including multi-owner job
    # lease recovery transactions.
    owner_tables = {
        "activities": ("user_id", "(TG_OP <> 'UPDATE' OR ROW(OLD.name,OLD.date,OLD.activity_type,OLD.processed,OLD.unmapped_points) IS DISTINCT FROM ROW(NEW.name,NEW.date,NEW.activity_type,NEW.processed,NEW.unmapped_points))", "activity"),
        "activity_sources": ("account_id", "(TG_OP <> 'UPDATE' OR ROW(OLD.activity_id,OLD.source_kind,OLD.revision,OLD.status,OLD.last_error) IS DISTINCT FROM ROW(NEW.activity_id,NEW.source_kind,NEW.revision,NEW.status,NEW.last_error))", "source"),
        "import_batches": ("account_id", "(TG_OP <> 'UPDATE' OR OLD.state IS DISTINCT FROM NEW.state)", "batch"),
        "import_batch_items": ("account_id", "(TG_OP <> 'UPDATE' OR ROW(OLD.status,OLD.source_id,OLD.duplicate,OLD.error_code) IS DISTINCT FROM ROW(NEW.status,NEW.source_id,NEW.duplicate,NEW.error_code))", "batch_item"),
        "coverage_source_runs": ("account_id", "(TG_OP <> 'UPDATE' OR ROW(OLD.status,OLD.sample_count,OLD.supported_sample_count,OLD.unsupported_sample_count,OLD.matched_node_count,OLD.last_error) IS DISTINCT FROM ROW(NEW.status,NEW.sample_count,NEW.supported_sample_count,NEW.unsupported_sample_count,NEW.matched_node_count,NEW.last_error))", "coverage_run"),
        "account_dataset_coverage": ("account_id", "(TG_OP <> 'UPDATE' OR ROW(OLD.status,OLD.pending_sources,OLD.failed_sources,OLD.visited_node_count,OLD.unsupported_sample_count,OLD.progress_revision) IS DISTINCT FROM ROW(NEW.status,NEW.pending_sources,NEW.failed_sources,NEW.visited_node_count,NEW.unsupported_sample_count,NEW.progress_revision))", "coverage_summary"),
        "manual_street_completions": ("account_id", "(TG_OP <> 'UPDATE' OR OLD.reason IS DISTINCT FROM NEW.reason)", "manual_completion"),
    }
    for table, (owner_column, changed, suffix) in owner_tables.items():
        op.execute(f"""
            CREATE FUNCTION collect_sync_revision_{suffix}() RETURNS trigger AS $$
            BEGIN
                IF TG_OP='UPDATE' AND NOT {changed} THEN RETURN NULL; END IF;
                IF TG_OP='DELETE' THEN
                    PERFORM queue_sync_revision_owner(OLD.{owner_column});
                ELSE
                    PERFORM queue_sync_revision_owner(NEW.{owner_column});
                END IF;
                RETURN NULL;
            END
            $$ LANGUAGE plpgsql
        """)
        function = f"collect_sync_revision_{suffix}"
        op.execute(f"""CREATE TRIGGER trg_{table}_sync_revision_collect
            AFTER INSERT OR UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION {function}()""")
        op.execute(f"""CREATE CONSTRAINT TRIGGER trg_{table}_sync_revision_flush
            AFTER INSERT OR UPDATE OR DELETE ON {table}
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION flush_sync_revision_owners()""")

    # Job lease heartbeats are deliberately excluded: only visible state or
    # retry/error changes advance the owner's revision.
    op.execute("""CREATE FUNCTION collect_sync_revision_job() RETURNS trigger AS $$
        BEGIN
            IF TG_OP='UPDATE' AND ROW(OLD.status,OLD.attempts,OLD.last_error)
                IS NOT DISTINCT FROM ROW(NEW.status,NEW.attempts,NEW.last_error) THEN RETURN NULL; END IF;
            IF COALESCE(NEW.kind,OLD.kind)='delete_private_object' THEN RETURN NULL; END IF;
            IF TG_OP='DELETE' THEN
                PERFORM queue_sync_revision_owner(OLD.account_id);
            ELSE
                PERFORM queue_sync_revision_owner(NEW.account_id);
            END IF;
            RETURN NULL;
        END
        $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER trg_jobs_sync_revision_collect AFTER INSERT OR UPDATE OR DELETE ON jobs
        FOR EACH ROW EXECUTE FUNCTION collect_sync_revision_job()""")
    op.execute("""CREATE CONSTRAINT TRIGGER trg_jobs_sync_revision_flush AFTER INSERT OR UPDATE OR DELETE ON jobs
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION flush_sync_revision_owners()""")

    op.execute("""CREATE FUNCTION collect_sync_revision_account_delete() RETURNS trigger AS $$
        BEGIN
            PERFORM queue_sync_revision_owner(OLD.id);
            RETURN NULL;
        END
        $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER trg_accounts_sync_revision_collect AFTER DELETE ON accounts
        FOR EACH ROW EXECUTE FUNCTION collect_sync_revision_account_delete()""")
    op.execute("""CREATE CONSTRAINT TRIGGER trg_accounts_sync_revision_flush AFTER DELETE ON accounts
        DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION flush_sync_revision_owners()""")

    op.execute("""CREATE FUNCTION bump_sync_geography_revision() RETURNS trigger AS $$
        BEGIN
            IF TG_OP='UPDATE' AND ROW(OLD.status,OLD.region,OLD.source_checksum,OLD.eligibility_rule_version,
                OLD.selection_key,OLD.coverage_mode) IS NOT DISTINCT FROM ROW(NEW.status,NEW.region,
                NEW.source_checksum,NEW.eligibility_rule_version,NEW.selection_key,NEW.coverage_mode) THEN
                RETURN NULL;
            END IF;
            UPDATE sync_geography_revision SET revision=revision+1 WHERE singleton=TRUE;
            RETURN NULL;
        END
        $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER trg_map_datasets_sync_revision AFTER INSERT OR UPDATE OR DELETE ON map_datasets
        FOR EACH ROW EXECUTE FUNCTION bump_sync_geography_revision()""")


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS trg_map_datasets_sync_revision ON map_datasets")
    op.execute("DROP FUNCTION IF EXISTS bump_sync_geography_revision()")
    op.execute("DROP TRIGGER IF EXISTS trg_accounts_sync_revision_flush ON accounts")
    op.execute("DROP TRIGGER IF EXISTS trg_accounts_sync_revision_collect ON accounts")
    op.execute("DROP FUNCTION IF EXISTS collect_sync_revision_account_delete()")
    for table, marker in (("activities", "activity"), ("activity_sources", "source"),
                          ("import_batches", "batch"), ("import_batch_items", "batch_item"),
                          ("coverage_source_runs", "coverage_run"), ("account_dataset_coverage", "coverage_summary"),
                          ("manual_street_completions", "manual_completion"), ("jobs", "job")):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_sync_revision_flush ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_sync_revision_collect ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS collect_sync_revision_{marker}()")
    op.execute("DROP FUNCTION IF EXISTS flush_sync_revision_owners()")
    op.execute("DROP FUNCTION IF EXISTS queue_sync_revision_owner(VARCHAR)")
    op.execute("DROP TABLE IF EXISTS sync_revision_events")
    op.execute("DROP TABLE IF EXISTS sync_geography_revision")
    op.execute("DROP TABLE IF EXISTS sync_account_revisions")
