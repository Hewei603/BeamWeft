"""Keep retried scientific records unique within their case."""
from alembic import op
import sqlalchemy as sa

revision = "20261117_0013"
down_revision = "20261117_0012"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("generated_contents", sa.Column("operation_key", sa.String(80)))
    op.create_index("ux_generated_case_operation", "generated_contents", ["case_id", "operation_key"], unique=True)


def downgrade():
    op.drop_index("ux_generated_case_operation", table_name="generated_contents")
    op.drop_column("generated_contents", "operation_key")
