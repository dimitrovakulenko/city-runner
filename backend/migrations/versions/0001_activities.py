from alembic import op
import sqlalchemy as sa

revision = "0001_activities"
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.create_table(
        "activities",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.String(255), nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("date", sa.String(40)),
        sa.Column("activity_type", sa.String(80)),
        sa.Column("tracks", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("timestamps", sa.JSON(), nullable=False, server_default="[]"),
    )
    op.execute("ALTER TABLE activities ADD COLUMN location geometry(Point, 4326)")
    op.create_index("ix_activities_user_date_id", "activities", ["user_id", "date", "id"])

def downgrade():
    op.drop_table("activities")
