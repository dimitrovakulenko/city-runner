"""Manual street completions scoped to an OSM dataset version."""

from alembic import op


revision = "0008_corrections"
down_revision = "0007_map"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
        CREATE TABLE manual_street_completions (
            account_id VARCHAR(255) NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            dataset_id BIGINT NOT NULL,
            street_id BIGINT NOT NULL,
            reason VARCHAR(500) NOT NULL CHECK (length(btrim(reason)) BETWEEN 1 AND 500),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (account_id,dataset_id,street_id),
            FOREIGN KEY (dataset_id,street_id) REFERENCES streets(dataset_id,id) ON DELETE CASCADE
        )
    """)
    op.execute("CREATE INDEX ix_manual_street_completions_dataset ON manual_street_completions(dataset_id,street_id)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_manual_street_completions_dataset")
    op.execute("DROP TABLE IF EXISTS manual_street_completions")
