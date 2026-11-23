"""Persist private optical presets independently of the source experiment."""
from alembic import op
import sqlalchemy as sa

revision = "20261118_0014"
down_revision = "20261117_0013"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("component_model_revisions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("owner_key", sa.String(80), nullable=False),
        sa.Column("family", sa.String(36), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("receipt", sa.String(64), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.UniqueConstraint("owner_key", "family", "version", name="uq_component_model_version"),
        sa.UniqueConstraint("owner_key", "receipt", name="uq_component_model_capture"))
    op.create_index("ix_component_model_revisions_owner_key", "component_model_revisions", ["owner_key"])


def downgrade():
    op.drop_table("component_model_revisions")
