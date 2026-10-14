"""Capture immutable municipal selector rules on shared public tasks."""

from alembic import op

revision = "0018_municipal_selectors"
down_revision = "0017_city_provisioning"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""ALTER TABLE city_public_tasks
        ADD COLUMN selector_profile VARCHAR(48) NOT NULL DEFAULT 'admin-levels-v1'
            CHECK (selector_profile ~ '^[a-z][a-z0-9-]{0,47}$'),
        ADD COLUMN selection_scope VARCHAR(5)
            CHECK (selection_scope IS NULL OR (task_kind='city' AND country_code='CA'
                AND selection_scope ~ '^CA-(AB|BC|MB|NB|NL|NS|NT|NU|ON|PE|QC|SK|YT)$'))""")


def downgrade():
    op.execute("ALTER TABLE city_public_tasks DROP COLUMN selection_scope, DROP COLUMN selector_profile")
