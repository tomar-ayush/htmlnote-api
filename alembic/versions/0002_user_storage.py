from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_storage",
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("endpoint_url", sa.String(512), nullable=False),
        sa.Column("region", sa.String(64), nullable=False),
        sa.Column("bucket", sa.String(255), nullable=False),
        sa.Column("access_key_id", sa.String(255), nullable=False),
        sa.Column("secret_key_enc", sa.String(1024), nullable=False),
        sa.Column("prefix", sa.String(255), nullable=False),
        sa.Column("force_path_style", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_storage")
