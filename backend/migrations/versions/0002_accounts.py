from alembic import op
import sqlalchemy as sa

revision = "0002_accounts"
down_revision = "0001_activities"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute("""INSERT INTO accounts (id)
        SELECT DISTINCT user_id FROM activities ON CONFLICT (id) DO NOTHING""")
    op.create_table(
        "login_identities",
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("account_id", sa.String(255), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("provider", "subject"),
        sa.CheckConstraint("provider IN ('google', 'apple')", name="ck_login_identities_provider"),
    )
    op.create_table(
        "login_challenges",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(16), nullable=False),
        sa.Column("nonce", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("provider IN ('google', 'apple')", name="ck_login_challenges_provider"),
        sa.CheckConstraint("length(nonce) = 64", name="ck_login_challenges_nonce_length"),
    )
    op.create_table(
        "sessions",
        sa.Column("token_digest", sa.CHAR(64), primary_key=True),
        sa.Column("account_id", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.CheckConstraint("length(token_digest) = 64", name="ck_sessions_digest_length"),
    )
    op.create_index("ix_sessions_account_id", "sessions", ["account_id"])
    op.create_foreign_key(
        "fk_activities_user_id_accounts", "activities", "accounts", ["user_id"], ["id"]
    )


def downgrade():
    op.drop_constraint("fk_activities_user_id_accounts", "activities", type_="foreignkey")
    op.drop_index("ix_sessions_account_id", table_name="sessions")
    op.drop_table("sessions")
    op.drop_table("login_challenges")
    op.drop_table("login_identities")
    op.drop_table("accounts")
