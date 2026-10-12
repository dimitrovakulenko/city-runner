"""Index exact recording identities and retain owner deletion watermarks."""

from alembic import op
import sqlalchemy as sa


revision = "0014_recording_groups"
down_revision = "0013_activity_statistics"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("activities", sa.Column("recording_fingerprint", sa.CHAR(64), nullable=True))
    op.add_column("activities", sa.Column(
        "recording_fingerprint_version", sa.SmallInteger(), nullable=False, server_default="0",
    ))
    op.create_check_constraint(
        "ck_activities_recording_fingerprint_version", "activities",
        "recording_fingerprint_version IN (0,1) AND "
        "(recording_fingerprint IS NULL OR recording_fingerprint_version=1)",
    )
    op.create_index(
        "uq_activities_user_recording_fingerprint", "activities",
        ["user_id", "recording_fingerprint"], unique=True,
        postgresql_where=sa.text("recording_fingerprint IS NOT NULL"),
    )
    op.execute("""
        CREATE TABLE recording_identity_deletions (
            account_id VARCHAR(255) NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            fingerprint CHAR(64) NOT NULL,
            fingerprint_version SMALLINT NOT NULL CHECK (fingerprint_version=1),
            deleted_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
            PRIMARY KEY (account_id,fingerprint,fingerprint_version)
        )
    """)
    op.execute("CREATE INDEX ix_recording_identity_deletions_deleted_at "
               "ON recording_identity_deletions(account_id,deleted_at)")


def downgrade():
    op.execute("DROP TABLE IF EXISTS recording_identity_deletions")
    op.drop_index("uq_activities_user_recording_fingerprint", table_name="activities")
    op.drop_constraint("ck_activities_recording_fingerprint_version", "activities", type_="check")
    op.drop_column("activities", "recording_fingerprint_version")
    op.drop_column("activities", "recording_fingerprint")
