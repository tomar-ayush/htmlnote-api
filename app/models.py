from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuthSession(Base):
    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class UserStorage(Base):
    __tablename__ = "user_storage"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    endpoint_url: Mapped[str] = mapped_column(String(512), default="")
    region: Mapped[str] = mapped_column(String(64), default="auto")
    bucket: Mapped[str] = mapped_column(String(255))
    access_key_id: Mapped[str] = mapped_column(String(255))
    secret_key_enc: Mapped[str] = mapped_column(String(1024))
    prefix: Mapped[str] = mapped_column(String(255), default="")
    force_path_style: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SignupOtp(Base):
    __tablename__ = "signup_otps"

    email: Mapped[str] = mapped_column(String(254), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    otp_hash: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SharedLink(Base):
    __tablename__ = "shared_links"
    __table_args__ = (UniqueConstraint("user_id", "page_path", name="uq_shared_links_user_page"),)

    token: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(32), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    page_path: Mapped[str] = mapped_column(String(768))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
