from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "signup_otps",
        sa.Column("email", sa.String(254), primary_key=True),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("otp_hash", sa.String(64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_signup_otps_expires_at", "signup_otps", ["expires_at"])


def downgrade() -> None:
    op.drop_table("signup_otps")
