"""Persist bounded GPS-derived activity statistics and revision changes."""

from alembic import op
import sqlalchemy as sa


revision = "0013_activity_statistics"
down_revision = "0012_sync_status"
branch_labels = None
depends_on = None


def _activity_revision_function(fields: str) -> str:
    old_fields = ",".join(f"OLD.{field}" for field in fields.split(","))
    new_fields = ",".join(f"NEW.{field}" for field in fields.split(","))
    return f"""CREATE OR REPLACE FUNCTION collect_sync_revision_activity() RETURNS trigger AS $$
        BEGIN
            IF TG_OP='UPDATE' AND ROW({old_fields}) IS NOT DISTINCT FROM ROW({new_fields}) THEN
                RETURN NULL;
            END IF;
            IF TG_OP='DELETE' THEN
                PERFORM queue_sync_revision_owner(OLD.user_id);
            ELSE
                PERFORM queue_sync_revision_owner(NEW.user_id);
            END IF;
            RETURN NULL;
        END
        $$ LANGUAGE plpgsql"""


_BASE_FIELDS = "name,date,activity_type,processed,unmapped_points"
_STAT_FIELDS = f"{_BASE_FIELDS},distance_m,duration_s,pace_s_per_km,statistics_version"


def upgrade():
    op.add_column("activities", sa.Column("distance_m", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("duration_s", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("pace_s_per_km", sa.Float(), nullable=True))
    op.add_column("activities", sa.Column("statistics_version", sa.Integer(), nullable=False, server_default="0"))
    op.create_check_constraint("ck_activities_distance_m_finite_nonnegative", "activities",
                               "distance_m IS NULL OR (distance_m >= 0 AND distance_m < 'Infinity'::float8)")
    op.create_check_constraint("ck_activities_duration_s_finite_nonnegative", "activities",
                               "duration_s IS NULL OR (duration_s >= 0 AND duration_s < 'Infinity'::float8)")
    op.create_check_constraint("ck_activities_pace_s_per_km_finite_nonnegative", "activities",
                               "pace_s_per_km IS NULL OR (pace_s_per_km >= 0 AND pace_s_per_km < 'Infinity'::float8)")
    op.create_check_constraint("ck_activities_statistics_version", "activities",
                               "statistics_version IN (0,1)")
    op.execute(_activity_revision_function(_STAT_FIELDS))


def downgrade():
    op.execute(_activity_revision_function(_BASE_FIELDS))
    op.drop_constraint("ck_activities_statistics_version", "activities", type_="check")
    op.drop_constraint("ck_activities_pace_s_per_km_finite_nonnegative", "activities", type_="check")
    op.drop_constraint("ck_activities_duration_s_finite_nonnegative", "activities", type_="check")
    op.drop_constraint("ck_activities_distance_m_finite_nonnegative", "activities", type_="check")
    op.drop_column("activities", "statistics_version")
    op.drop_column("activities", "pace_s_per_km")
    op.drop_column("activities", "duration_s")
    op.drop_column("activities", "distance_m")
