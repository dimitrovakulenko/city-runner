"""Durable shared public-city discovery and owner-scoped preparation."""

from alembic import op


revision = "0017_city_provisioning"
down_revision = "0016_region_imports"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""CREATE TABLE city_provider_admission (
        singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
        utc_day DATE NOT NULL DEFAULT (clock_timestamp() AT TIME ZONE 'UTC')::date,
        requests_reserved INTEGER NOT NULL DEFAULT 0 CHECK (requests_reserved BETWEEN 0 AND 100),
        bytes_reserved BIGINT NOT NULL DEFAULT 0 CHECK (bytes_reserved BETWEEN 0 AND 268435456),
        next_request_at TIMESTAMPTZ NOT NULL DEFAULT (clock_timestamp()-interval '1 second'),
        active_task_key TEXT,
        active_lease_until TIMESTAMPTZ,
        active_lease_token VARCHAR(32),
        CHECK ((active_task_key IS NULL) = (active_lease_until IS NULL)),
        CHECK ((active_task_key IS NULL) = (active_lease_token IS NULL))
    )""")
    op.execute("INSERT INTO city_provider_admission(singleton) VALUES (TRUE)")
    op.execute("""CREATE TABLE city_public_tasks (
        task_key VARCHAR(255) PRIMARY KEY,
        task_kind VARCHAR(16) NOT NULL CHECK (task_kind IN ('world','country','city')),
        country_code CHAR(2),
        admin_levels JSONB,
        relation_id BIGINT,
        city_name TEXT,
        status VARCHAR(16) NOT NULL DEFAULT 'queued'
            CHECK (status IN ('queued','running','succeeded','failed','unsupported')),
        attempts SMALLINT NOT NULL DEFAULT 0 CHECK (attempts BETWEEN 0 AND 5),
        available_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        lease_token VARCHAR(32),
        leased_until TIMESTAMPTZ,
        safe_error VARCHAR(64),
        source_checksum CHAR(64),
        source_timestamp TIMESTAMPTZ,
        result_json JSONB,
        map_import_task_id BIGINT REFERENCES map_import_tasks(id) ON DELETE SET NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        CHECK ((status='running' AND lease_token IS NOT NULL AND leased_until IS NOT NULL) OR
               (status<>'running' AND lease_token IS NULL AND leased_until IS NULL)),
        CHECK ((task_kind='world' AND country_code IS NULL AND relation_id IS NULL) OR
               (task_kind='country' AND country_code ~ '^[A-Z]{2}$' AND relation_id IS NULL
                    AND jsonb_typeof(admin_levels)='array') OR
               (task_kind='city' AND country_code ~ '^[A-Z]{2}$' AND relation_id > 0))
    )""")
    op.execute("CREATE INDEX ix_city_public_tasks_claim ON city_public_tasks(status,available_at,created_at)")
    op.execute("""CREATE TABLE activity_preparation_requests (
        account_id VARCHAR(255) NOT NULL,
        source_id BIGINT NOT NULL,
        source_revision INTEGER NOT NULL CHECK (source_revision > 0),
        state VARCHAR(16) NOT NULL DEFAULT 'pending'
            CHECK (state IN ('pending','ready','failed','unsupported')),
        phase VARCHAR(16) NOT NULL DEFAULT 'catalog'
            CHECK (phase IN ('catalog','country','city','verify','done')),
        country_codes JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(country_codes)='array'),
        country_cursor CHAR(2),
        candidate_cursor BIGINT,
        unsupported_sample_count INTEGER,
        safe_error VARCHAR(64),
        created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (account_id,source_id,source_revision),
        FOREIGN KEY (account_id,source_id) REFERENCES activity_sources(account_id,id) ON DELETE CASCADE
    )""")
    op.execute("CREATE INDEX ix_activity_preparation_owner_state ON activity_preparation_requests(account_id,state,updated_at)")
    op.execute("CREATE INDEX ix_activity_preparation_source ON activity_preparation_requests(source_id,source_revision)")
    op.execute("""CREATE TABLE activity_preparation_countries (
        account_id VARCHAR(255) NOT NULL,
        source_id BIGINT NOT NULL,
        source_revision INTEGER NOT NULL,
        country_code CHAR(2) NOT NULL,
        task_key VARCHAR(255) NOT NULL REFERENCES city_public_tasks(task_key) ON DELETE RESTRICT,
        state VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','done','unsupported')),
        candidate_cursor BIGINT,
        PRIMARY KEY (account_id,source_id,source_revision,country_code),
        FOREIGN KEY (account_id,source_id,source_revision)
            REFERENCES activity_preparation_requests(account_id,source_id,source_revision) ON DELETE CASCADE
    )""")
    op.execute("""CREATE TABLE activity_preparation_cities (
        account_id VARCHAR(255) NOT NULL,
        source_id BIGINT NOT NULL,
        source_revision INTEGER NOT NULL,
        relation_id BIGINT NOT NULL,
        name TEXT NOT NULL,
        task_key VARCHAR(255) NOT NULL REFERENCES city_public_tasks(task_key) ON DELETE RESTRICT,
        dataset_id BIGINT REFERENCES map_datasets(id) ON DELETE SET NULL,
        state VARCHAR(16) NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','importing','ready','failed','unsupported')),
        touched BOOLEAN NOT NULL DEFAULT FALSE,
        PRIMARY KEY (account_id,source_id,source_revision,relation_id),
        FOREIGN KEY (account_id,source_id,source_revision)
            REFERENCES activity_preparation_requests(account_id,source_id,source_revision) ON DELETE CASCADE
    )""")
    op.execute("CREATE INDEX ix_preparation_cities_owner ON activity_preparation_cities(account_id,state,name,relation_id)")
    op.execute("""CREATE TABLE source_dataset_applicability (
        account_id VARCHAR(255) NOT NULL,
        source_id BIGINT NOT NULL,
        source_revision INTEGER NOT NULL,
        dataset_id BIGINT NOT NULL REFERENCES map_datasets(id) ON DELETE CASCADE,
        applicable BOOLEAN NOT NULL,
        touched BOOLEAN NOT NULL,
        checked_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
        PRIMARY KEY (account_id,source_id,source_revision,dataset_id),
        FOREIGN KEY (account_id,source_id) REFERENCES activity_sources(account_id,id) ON DELETE CASCADE
    )""")
    op.execute("CREATE INDEX ix_source_dataset_applicability_dataset ON source_dataset_applicability(dataset_id,account_id,applicable)")
    op.execute("""CREATE FUNCTION collect_city_preparation_sync_revision() RETURNS trigger AS $$
        BEGIN
            IF TG_TABLE_NAME='activity_preparation_requests' THEN
                IF TG_OP='UPDATE' AND
                    ROW(OLD.state,OLD.phase,OLD.country_codes,OLD.country_cursor,OLD.candidate_cursor,
                        OLD.unsupported_sample_count,OLD.safe_error) IS NOT DISTINCT FROM
                    ROW(NEW.state,NEW.phase,NEW.country_codes,NEW.country_cursor,NEW.candidate_cursor,
                        NEW.unsupported_sample_count,NEW.safe_error) THEN RETURN NULL; END IF;
            END IF;
            IF TG_OP='DELETE' THEN PERFORM queue_sync_revision_owner(OLD.account_id);
            ELSE PERFORM queue_sync_revision_owner(NEW.account_id); END IF;
            RETURN NULL;
        END $$ LANGUAGE plpgsql""")
    for table in ("activity_preparation_requests", "activity_preparation_countries", "activity_preparation_cities", "source_dataset_applicability"):
        op.execute(f"""CREATE TRIGGER trg_{table}_sync_collect AFTER INSERT OR UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION collect_city_preparation_sync_revision()""")
        op.execute(f"""CREATE CONSTRAINT TRIGGER trg_{table}_sync_flush AFTER INSERT OR UPDATE OR DELETE ON {table}
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION flush_sync_revision_owners()""")
    op.execute("""CREATE FUNCTION bump_city_public_sync_revision() RETURNS trigger AS $$
        BEGIN
            IF TG_OP='UPDATE' AND ROW(OLD.status,OLD.safe_error,OLD.result_json,OLD.map_import_task_id)
                IS NOT DISTINCT FROM ROW(NEW.status,NEW.safe_error,NEW.result_json,NEW.map_import_task_id) THEN RETURN NULL; END IF;
            UPDATE sync_geography_revision SET revision=revision+1 WHERE singleton=TRUE;
            RETURN NULL;
        END $$ LANGUAGE plpgsql""")
    op.execute("""CREATE TRIGGER trg_city_public_sync_revision AFTER INSERT OR UPDATE OR DELETE ON city_public_tasks
        FOR EACH ROW EXECUTE FUNCTION bump_city_public_sync_revision()""")


def downgrade():
    op.execute("DROP TRIGGER IF EXISTS trg_city_public_sync_revision ON city_public_tasks")
    op.execute("DROP FUNCTION IF EXISTS bump_city_public_sync_revision()")
    for table in ("activity_preparation_requests", "activity_preparation_countries", "activity_preparation_cities", "source_dataset_applicability"):
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_sync_flush ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_sync_collect ON {table}")
    op.execute("DROP FUNCTION IF EXISTS collect_city_preparation_sync_revision()")
    op.execute("DROP TABLE IF EXISTS source_dataset_applicability")
    op.execute("DROP TABLE IF EXISTS activity_preparation_cities")
    op.execute("DROP TABLE IF EXISTS activity_preparation_countries")
    op.execute("DROP TABLE IF EXISTS activity_preparation_requests")
    op.execute("DROP TABLE IF EXISTS city_public_tasks")
    op.execute("DROP TABLE IF EXISTS city_provider_admission")
