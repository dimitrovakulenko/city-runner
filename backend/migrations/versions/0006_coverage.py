"""Per-source GPS support and rebuildable account coverage summaries."""

from alembic import op


revision = "0006_coverage"
down_revision = "0005_sources"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("ALTER TABLE activity_sources ADD CONSTRAINT uq_activity_sources_account_id_revision "
               "UNIQUE (account_id, id, revision)")
    op.execute("ALTER TABLE jobs ADD CONSTRAINT uq_jobs_account_id_id UNIQUE (account_id, id)")
    op.execute("""
        CREATE TABLE source_node_contributions (
            account_id VARCHAR(255) NOT NULL,
            source_id BIGINT NOT NULL,
            source_revision INTEGER NOT NULL,
            dataset_id BIGINT NOT NULL,
            node_id BIGINT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (account_id, source_id, source_revision, dataset_id, node_id),
            FOREIGN KEY (account_id, source_id, source_revision)
                REFERENCES activity_sources(account_id, id, revision) ON DELETE CASCADE,
            FOREIGN KEY (dataset_id, node_id)
                REFERENCES osm_nodes(dataset_id, osm_node_id) ON DELETE CASCADE
        )
    """)
    op.execute("CREATE INDEX ix_source_node_contributions_account_dataset_node "
               "ON source_node_contributions(account_id, dataset_id, node_id)")
    op.execute("CREATE INDEX ix_source_node_contributions_source "
               "ON source_node_contributions(account_id, source_id, source_revision, dataset_id)")
    op.execute("""
        CREATE TABLE coverage_source_runs (
            account_id VARCHAR(255) NOT NULL,
            source_id BIGINT NOT NULL,
            source_revision INTEGER NOT NULL,
            dataset_id BIGINT NOT NULL,
            job_id BIGINT NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued','running','succeeded','failed')),
            sample_count BIGINT NOT NULL DEFAULT 0 CHECK (sample_count >= 0),
            supported_sample_count BIGINT NOT NULL DEFAULT 0 CHECK (supported_sample_count >= 0),
            unsupported_sample_count BIGINT NOT NULL DEFAULT 0 CHECK (unsupported_sample_count >= 0),
            matched_node_count BIGINT NOT NULL DEFAULT 0 CHECK (matched_node_count >= 0),
            last_error VARCHAR(64),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (account_id, source_id, source_revision, dataset_id),
            FOREIGN KEY (account_id, source_id, source_revision)
                REFERENCES activity_sources(account_id, id, revision) ON DELETE CASCADE,
            FOREIGN KEY (dataset_id) REFERENCES map_datasets(id) ON DELETE CASCADE,
            FOREIGN KEY (account_id, job_id) REFERENCES jobs(account_id, id) ON DELETE CASCADE
        )
    """)
    op.execute("CREATE INDEX ix_coverage_source_runs_dataset_status "
               "ON coverage_source_runs(account_id, dataset_id, status, source_id)")
    op.execute("""
        CREATE TABLE account_dataset_coverage (
            account_id VARCHAR(255) NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            dataset_id BIGINT NOT NULL REFERENCES map_datasets(id) ON DELETE CASCADE,
            status VARCHAR(16) NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending','ready','failed')),
            pending_sources BIGINT NOT NULL DEFAULT 0 CHECK (pending_sources >= 0),
            failed_sources BIGINT NOT NULL DEFAULT 0 CHECK (failed_sources >= 0),
            visited_node_count BIGINT NOT NULL DEFAULT 0 CHECK (visited_node_count >= 0),
            unsupported_sample_count BIGINT NOT NULL DEFAULT 0 CHECK (unsupported_sample_count >= 0),
            progress_revision BIGINT NOT NULL DEFAULT 1 CHECK (progress_revision > 0),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (account_id, dataset_id)
        )
    """)
    op.execute("""
        CREATE TABLE account_street_coverage (
            account_id VARCHAR(255) NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            dataset_id BIGINT NOT NULL,
            street_id BIGINT NOT NULL,
            visited_node_count INTEGER NOT NULL CHECK (visited_node_count > 0),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (account_id, dataset_id, street_id),
            FOREIGN KEY (dataset_id, street_id) REFERENCES streets(dataset_id, id) ON DELETE CASCADE
        )
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION invalidate_source_coverage() RETURNS trigger AS $$
        DECLARE affected_dataset BIGINT;
        BEGIN
            IF TG_OP = 'UPDATE' AND NEW.revision = OLD.revision THEN
                RETURN NEW;
            END IF;
            FOR affected_dataset IN
                SELECT DISTINCT dataset_id FROM coverage_source_runs
                WHERE account_id=OLD.account_id AND source_id=OLD.id AND source_revision=OLD.revision
                UNION
                SELECT DISTINCT dataset_id FROM source_node_contributions
                WHERE account_id=OLD.account_id AND source_id=OLD.id AND source_revision=OLD.revision
            LOOP
                UPDATE account_dataset_coverage SET status='pending', visited_node_count=0,
                    unsupported_sample_count=0, pending_sources=GREATEST(pending_sources, 1),
                    progress_revision=progress_revision+1, updated_at=clock_timestamp()
                WHERE account_id=OLD.account_id AND dataset_id=affected_dataset;
                DELETE FROM account_street_coverage
                WHERE account_id=OLD.account_id AND dataset_id=affected_dataset;
            END LOOP;
            IF TG_OP = 'UPDATE' THEN
                UPDATE activities SET processed=false
                WHERE user_id=OLD.account_id AND id=OLD.activity_id;
                DELETE FROM source_node_contributions
                WHERE account_id=OLD.account_id AND source_id=OLD.id AND source_revision=OLD.revision;
                DELETE FROM coverage_source_runs
                WHERE account_id=OLD.account_id AND source_id=OLD.id AND source_revision=OLD.revision;
                RETURN NEW;
            END IF;
            RETURN OLD;
        END
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_activity_sources_invalidate_coverage
        BEFORE UPDATE OF revision OR DELETE ON activity_sources
        FOR EACH ROW EXECUTE FUNCTION invalidate_source_coverage()
    """)


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS trg_activity_sources_invalidate_coverage ON activity_sources")
    op.execute("DROP FUNCTION IF EXISTS invalidate_source_coverage()")
    op.execute("DROP TABLE IF EXISTS account_street_coverage")
    op.execute("DROP TABLE IF EXISTS account_dataset_coverage")
    op.execute("DROP INDEX IF EXISTS ix_coverage_source_runs_dataset_status")
    op.execute("DROP TABLE IF EXISTS coverage_source_runs")
    op.execute("DROP INDEX IF EXISTS ix_source_node_contributions_source")
    op.execute("DROP INDEX IF EXISTS ix_source_node_contributions_account_dataset_node")
    op.execute("DROP TABLE IF EXISTS source_node_contributions")
    op.execute("ALTER TABLE jobs DROP CONSTRAINT IF EXISTS uq_jobs_account_id_id")
    op.execute("ALTER TABLE activity_sources DROP CONSTRAINT IF EXISTS uq_activity_sources_account_id_revision")
