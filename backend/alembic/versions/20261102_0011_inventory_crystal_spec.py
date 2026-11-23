"""Preserve ordered crystal sections and absorber provenance.

Revision ID: 20261102_0011
Revises: 20260802_0010
"""
from alembic import op
import sqlalchemy as sa

revision = "20261102_0011"
down_revision = "20260802_0010"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("inventory_items", sa.Column("crystal_spec", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("inventory_items", "crystal_spec")
