from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_google_drive",
        sa.Column("user_id", sa.String(32), sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("google_email", sa.String(254), nullable=False, server_default=""),
        sa.Column("refresh_token_enc", sa.String(2048), nullable=False),
        sa.Column("access_token_enc", sa.String(2048), nullable=False, server_default=""),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("root_folder_id", sa.String(128), nullable=False),
        sa.Column("root_folder_name", sa.String(255), nullable=False, server_default="HTML Notebook"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column(
        "shared_links",
        sa.Column("provider", sa.String(16), nullable=False, server_default="s3"),
    )


def downgrade() -> None:
    op.drop_column("shared_links", "provider")
    op.drop_table("user_google_drive")
