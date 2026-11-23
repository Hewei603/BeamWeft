"""Protect cited originals and reserve attachment IDs after removal."""
from alembic import op
import sqlalchemy as sa

revision = "20261119_0015"
down_revision = "20261118_0014"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("attachments", sa.Column("evidence_use", sa.String(160)))
    op.create_table("attachment_id_watermark",
                    sa.Column("id", sa.Integer(), primary_key=True),
                    sa.Column("last_id", sa.Integer(), nullable=False))


def downgrade():
    op.drop_table("attachment_id_watermark")
    op.drop_column("attachments", "evidence_use")
