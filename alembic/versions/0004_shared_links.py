from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "shared_links",
        sa.Column("token", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("page_path", sa.String(768), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "page_path", name="uq_shared_links_user_page"),
    )
    op.create_index("ix_shared_links_user_id", "shared_links", ["user_id"])
    op.create_index("ix_shared_links_expires_at", "shared_links", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_shared_links_expires_at", table_name="shared_links")
    op.drop_index("ix_shared_links_user_id", table_name="shared_links")
    op.drop_table("shared_links")
