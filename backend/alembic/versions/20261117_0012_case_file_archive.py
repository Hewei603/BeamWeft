"""Preserve experiment directory paths and upload receipts."""
from alembic import op
import sqlalchemy as sa

revision = "20261117_0012"
down_revision = "20261102_0011"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("attachments", sa.Column("relative_path", sa.String(2048)))
    op.add_column("attachments", sa.Column("upload_id", sa.String(36)))
    op.add_column("attachments", sa.Column("index_note", sa.String(255)))
    op.create_index("ux_attachments_case_upload", "attachments", ["case_id", "upload_id"], unique=True)


def downgrade():
    op.drop_index("ux_attachments_case_upload", table_name="attachments")
    for name in ("index_note", "upload_id", "relative_path"):
        op.drop_column("attachments", name)
