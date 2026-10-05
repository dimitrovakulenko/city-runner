"""Account-owned source records for private GPX uploads."""

from alembic import op
import sqlalchemy as sa


revision = "0005_sources"
down_revision = "0004_geography"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint("uq_activities_user_id_id", "activities", ["user_id", "id"])
    op.create_table(
        "activity_sources",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("account_id", sa.String(255), nullable=False),
        sa.Column("activity_id", sa.BigInteger()),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_connection_id", sa.BigInteger()),
        sa.Column("external_id", sa.String(255)),
        sa.Column("content_hash", sa.CHAR(64)),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("private_object_key", sa.String(255)),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("last_error", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["account_id", "activity_id"], ["activities.user_id", "activities.id"],
            ondelete="CASCADE", name="fk_activity_sources_owned_activity",
        ),
        sa.UniqueConstraint("account_id", "id", name="uq_activity_sources_account_id_id"),
        sa.UniqueConstraint(
            "account_id", "source_kind", "content_hash", name="uq_activity_sources_account_kind_hash",
        ),
        sa.CheckConstraint("revision > 0", name="ck_activity_sources_revision"),
        sa.CheckConstraint("status IN ('queued','processing','succeeded','failed')",
                           name="ck_activity_sources_status"),
    )
    op.create_index("ix_activity_sources_account_status", "activity_sources", ["account_id", "status", "created_at"])
    op.create_index("ix_activity_sources_activity", "activity_sources", ["account_id", "activity_id"])
    op.execute("LOCK TABLE activities IN SHARE ROW EXCLUSIVE MODE")
    op.execute("""DO $$
        DECLARE
            sequence_name text;
            sequence_last bigint;
            sequence_called boolean;
            greatest_id bigint;
        BEGIN
            sequence_name := pg_get_serial_sequence('activities', 'id');
            IF sequence_name IS NOT NULL THEN
                EXECUTE format('SELECT last_value, is_called FROM %s', sequence_name::regclass)
                    INTO sequence_last, sequence_called;
                SELECT GREATEST(COALESCE(MAX(id), 0), 0) INTO greatest_id
                    FROM activities WHERE id > 0;
                IF greatest_id > sequence_last OR (greatest_id = sequence_last AND NOT sequence_called) THEN
                    PERFORM setval(sequence_name::regclass, greatest_id, true);
                END IF;
            END IF;
        END $$""")


def downgrade():
    op.drop_index("ix_activity_sources_activity", table_name="activity_sources")
    op.drop_index("ix_activity_sources_account_status", table_name="activity_sources")
    op.drop_table("activity_sources")
    op.drop_constraint("uq_activities_user_id_id", "activities", type_="unique")
