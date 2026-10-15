"""Keep authoring navigation inside single-use handoff state."""

from alembic import op

revision = "0020_analytics_handoff"
down_revision = "0019_analytics"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""ALTER TABLE analytics_handoff_tickets ADD COLUMN destination varchar(16)
        NOT NULL DEFAULT 'default' CHECK(destination IN ('default','sql','boards','dashboard')),
        ADD COLUMN dashboard_id bigint""")
    op.execute("CREATE INDEX ix_analytics_accounts_poll ON analytics_accounts(available_at) WHERE state<>'deleted'")


def downgrade():
    op.execute("DROP INDEX ix_analytics_accounts_poll")
    op.execute("ALTER TABLE analytics_handoff_tickets DROP COLUMN dashboard_id,DROP COLUMN destination")
