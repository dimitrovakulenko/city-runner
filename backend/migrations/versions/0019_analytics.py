"""Durable analytics projection, preferences and app-session-bound handoff."""

from alembic import op

revision = "0019_analytics"
down_revision = "0018_municipal_selectors"
branch_labels = None
depends_on = None


def upgrade():
    # No account FK: this durable cleanup record must survive account deletion.
    op.execute("""CREATE TABLE analytics_accounts (
        account_id varchar(255) PRIMARY KEY,
        owner_key char(64) NOT NULL UNIQUE,
        selected_dataset_id bigint,
        default_dashboard_id bigint,
        state varchar(16) NOT NULL DEFAULT 'pending'
          CHECK (state IN ('pending','processing','ready','failed','deleting','deleted')),
        generation uuid NOT NULL,
        source_revision text,
        lease_token uuid,
        leased_until timestamptz,
        available_at timestamptz NOT NULL DEFAULT now(),
        last_error varchar(64),
        updated_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("""CREATE TABLE analytics_handoff_tickets (
        token_digest char(64) PRIMARY KEY,
        app_session_digest char(64) NOT NULL REFERENCES sessions(token_digest) ON DELETE CASCADE,
        expires_at timestamptz NOT NULL,
        consumed_at timestamptz
    )""")
    op.execute("""CREATE TABLE analytics_sessions (
        token_digest char(64) PRIMARY KEY,
        app_session_digest char(64) NOT NULL REFERENCES sessions(token_digest) ON DELETE CASCADE,
        expires_at timestamptz NOT NULL,
        revoked_at timestamptz
    )""")
    op.execute("CREATE INDEX ix_analytics_tickets_expiry ON analytics_handoff_tickets(expires_at)")
    op.execute("CREATE INDEX ix_analytics_sessions_expiry ON analytics_sessions(expires_at)")


def downgrade():
    op.execute("DROP TABLE analytics_sessions")
    op.execute("DROP TABLE analytics_handoff_tickets")
    op.execute("DROP TABLE analytics_accounts")
