"""Cache indexed display geometry for owner activity viewports."""

from alembic import op

revision = "0007_map"
down_revision = "0006_coverage"
branch_labels = None
depends_on = None


TRACK_GEOMETRY_FUNCTION = r"""
CREATE OR REPLACE FUNCTION activity_track_geometry(tracks_json jsonb)
RETURNS geometry LANGUAGE plpgsql IMMUTABLE STRICT AS $$
DECLARE
    segment jsonb;
    coordinate jsonb;
    point_geom geometry;
    segment_points geometry[];
    parts geometry[] := ARRAY[]::geometry[];
    lon double precision;
    lat double precision;
BEGIN
    IF jsonb_typeof(tracks_json) <> 'array' THEN
        RETURN NULL;
    END IF;
    FOR segment IN SELECT value FROM jsonb_array_elements(tracks_json)
    LOOP
        IF jsonb_typeof(segment) <> 'array' THEN
            RETURN NULL;
        END IF;
        segment_points := ARRAY[]::geometry[];
        FOR coordinate IN SELECT value FROM jsonb_array_elements(segment)
        LOOP
            IF jsonb_typeof(coordinate) <> 'array' OR jsonb_array_length(coordinate) <> 2
               OR jsonb_typeof(coordinate->0) <> 'number'
               OR jsonb_typeof(coordinate->1) <> 'number' THEN
                RETURN NULL;
            END IF;
            lon := (coordinate->>0)::double precision;
            lat := (coordinate->>1)::double precision;
            IF lon IS NULL OR lat IS NULL OR lon < -180 OR lon > 180 OR lat < -90 OR lat > 90 THEN
                RETURN NULL;
            END IF;
            point_geom := ST_SetSRID(ST_MakePoint(lon, lat), 4326);
            segment_points := array_append(segment_points, point_geom);
        END LOOP;
        IF cardinality(segment_points) = 1 THEN
            parts := array_append(parts, segment_points[1]);
        ELSIF cardinality(segment_points) > 1 THEN
            parts := array_append(parts, ST_MakeLine(segment_points));
        END IF;
    END LOOP;
    IF cardinality(parts) = 0 THEN
        RETURN NULL;
    ELSIF cardinality(parts) = 1 THEN
        RETURN parts[1];
    END IF;
    RETURN ST_Collect(parts);
EXCEPTION WHEN data_exception OR numeric_value_out_of_range THEN
    RETURN NULL;
END
$$;
"""


TRIGGER_FUNCTION = r"""
CREATE OR REPLACE FUNCTION set_activity_track_geometry()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.track_geometry := activity_track_geometry(NEW.tracks::jsonb);
    RETURN NEW;
END
$$;
"""


def upgrade():
    op.execute("ALTER TABLE activities ADD COLUMN track_geometry geometry(Geometry,4326)")
    op.execute(TRACK_GEOMETRY_FUNCTION)
    op.execute(TRIGGER_FUNCTION)
    op.execute("""
        UPDATE activities SET track_geometry=activity_track_geometry(tracks::jsonb)
        WHERE tracks IS NOT NULL
    """)
    op.execute("""
        CREATE TRIGGER trg_activities_track_geometry
        BEFORE INSERT OR UPDATE OF tracks ON activities
        FOR EACH ROW EXECUTE FUNCTION set_activity_track_geometry()
    """)
    op.execute("CREATE INDEX ix_activities_track_geometry_gist ON activities USING GIST(track_geometry)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_activities_track_geometry_gist")
    op.execute("DROP TRIGGER IF EXISTS trg_activities_track_geometry ON activities")
    op.execute("DROP FUNCTION IF EXISTS set_activity_track_geometry()")
    op.execute("DROP FUNCTION IF EXISTS activity_track_geometry(jsonb)")
    op.execute("ALTER TABLE activities DROP COLUMN IF EXISTS track_geometry")
