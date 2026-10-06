"""Account-owned pedestrian route plans and global provider quota."""

from alembic import op


revision = "0010_routes"
down_revision = "0009_import_batches"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE saved_routes (
            id UUID PRIMARY KEY,
            account_id VARCHAR(255) NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            name VARCHAR(100) NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 100),
            revision INTEGER NOT NULL DEFAULT 1 CHECK (revision BETWEEN 1 AND 2147483647),
            waypoints JSONB NOT NULL CHECK (
                jsonb_typeof(waypoints)='array' AND jsonb_array_length(waypoints) BETWEEN 2 AND 20
            ),
            geometry geometry(LineString,4326) NOT NULL CHECK (
                ST_NPoints(geometry) BETWEEN 2 AND 10000 AND ST_IsValid(geometry)
                AND ST_XMin(Box3D(geometry)) BETWEEN -180 AND 180
                AND ST_XMax(Box3D(geometry)) BETWEEN -180 AND 180
                AND ST_YMin(Box3D(geometry)) BETWEEN -90 AND 90
                AND ST_YMax(Box3D(geometry)) BETWEEN -90 AND 90
            ),
            distance_m DOUBLE PRECISION NOT NULL CHECK (distance_m >= 0 AND distance_m < 'Infinity'::float8),
            duration_s DOUBLE PRECISION NOT NULL CHECK (duration_s >= 0 AND duration_s < 'Infinity'::float8),
            provider VARCHAR(32) NOT NULL,
            attribution JSONB NOT NULL,
            routed_at TIMESTAMPTZ NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            UNIQUE (account_id,id)
        )
    """)
    op.execute("CREATE INDEX ix_saved_routes_owner_updated ON saved_routes(account_id,updated_at DESC,id DESC)")
    op.execute("""
        CREATE TABLE routing_provider_quota (
            provider VARCHAR(32) PRIMARY KEY,
            next_allowed_at TIMESTAMPTZ NOT NULL DEFAULT '-infinity'::timestamptz
        )
    """)
    op.execute("INSERT INTO routing_provider_quota(provider) VALUES ('fossgis')")


def downgrade():
    op.execute("DROP TABLE IF EXISTS routing_provider_quota")
    op.execute("DROP INDEX IF EXISTS ix_saved_routes_owner_updated")
    op.execute("DROP TABLE IF EXISTS saved_routes")
