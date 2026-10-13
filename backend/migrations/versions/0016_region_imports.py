"""Durable shared geography import and source backfill tasks."""

from alembic import op


revision = "0016_region_imports"
down_revision = "0015_import_histories"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE map_import_tasks (
        id BIGSERIAL PRIMARY KEY,
        task_type VARCHAR(24) NOT NULL CHECK (task_type IN ('osm_import','coverage_backfill')),
        phase VARCHAR(16) NOT NULL CHECK (phase IN ('import','backfill')),
        region VARCHAR(128),
        object_key VARCHAR(128),
        source_checksum CHAR(64),
        source_timestamp TIMESTAMPTZ,
        city_relation_ids JSONB,
        eligibility_rule_version VARCHAR(64),
        selection_key CHAR(64),
        coverage_mode VARCHAR(16),
        coverage_evidence TEXT,
        dataset_id BIGINT,
        source_cursor BIGINT,
        status VARCHAR(16) NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued','running','backfilling','succeeded','failed')),
        attempts SMALLINT NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 5),
        max_attempts SMALLINT NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 5),
        available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        lease_token VARCHAR(32),
        leased_until TIMESTAMPTZ,
        last_error VARCHAR(64),
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        CHECK ((status='running' AND lease_token IS NOT NULL AND leased_until IS NOT NULL) OR
               (status<>'running' AND lease_token IS NULL AND leased_until IS NULL)),
        CHECK ((task_type='osm_import' AND phase IN ('import','backfill') AND region IS NOT NULL
                AND object_key IS NOT NULL AND source_checksum ~ '^[0-9a-f]{64}$'
                AND city_relation_ids IS NOT NULL AND eligibility_rule_version IS NOT NULL
                AND selection_key ~ '^[0-9a-f]{64}$' AND coverage_mode IN ('complete','sampled')
                AND coverage_evidence IS NOT NULL) OR
               (task_type='coverage_backfill' AND phase='backfill' AND dataset_id IS NOT NULL
                AND object_key IS NULL AND source_checksum IS NULL)),
        CHECK (object_key IS NULL OR object_key ~ '^[0-9a-f]{32}-[0-9a-f]{12}\\.osm$')
    )""")
    op.execute("""CREATE UNIQUE INDEX uq_map_import_tasks_identity
        ON map_import_tasks(region,source_checksum,coverage_mode,selection_key,eligibility_rule_version)
        WHERE task_type='osm_import'""")
    op.execute("""CREATE UNIQUE INDEX uq_map_import_tasks_dataset_backfill
        ON map_import_tasks(dataset_id) WHERE task_type='coverage_backfill'""")
    op.execute("CREATE INDEX ix_map_import_tasks_claim ON map_import_tasks(status,available_at,id)")
    op.execute("""INSERT INTO map_import_tasks(task_type,phase,dataset_id,status)
        SELECT 'coverage_backfill','backfill',id,'queued' FROM map_datasets
        WHERE status='active' AND coverage_mode='complete' ON CONFLICT DO NOTHING""")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_map_import_tasks_claim")
    op.execute("DROP INDEX IF EXISTS uq_map_import_tasks_dataset_backfill")
    op.execute("DROP INDEX IF EXISTS uq_map_import_tasks_identity")
    op.execute("DROP TABLE IF EXISTS map_import_tasks")
