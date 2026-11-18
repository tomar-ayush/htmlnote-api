import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import auth, gdrive, storage
from .db import get_db
from .models import SharedLink, User, UserGoogleDrive, UserStorage
from .store import Store, StoreError

router = APIRouter(prefix="/api/share", tags=["share"])


class ShareCreateIn(BaseModel):
    path: str
    expiresHours: int | None = None


def active_provider(user_id: str, db: Session) -> str:
    drive = db.get(UserGoogleDrive, user_id)
    return "gdrive" if drive and drive.active else "s3"


def store_for_user(user_id: str, db: Session, provider: str | None = None) -> Store | gdrive.DriveStore:
    chosen = provider or active_provider(user_id, db)
    if chosen == "gdrive":
        return gdrive.drive_store_for_user(user_id, db)
    cfg = db.get(UserStorage, user_id)
    if cfg:
        return Store(
            endpoint_url=cfg.endpoint_url,
            region=cfg.region,
            bucket=cfg.bucket,
            access_key_id=cfg.access_key_id,
            secret_access_key=storage.decrypt_secret(cfg.secret_key_enc),
            prefix=cfg.prefix,
            force_path_style=cfg.force_path_style,
        )
    if storage.platform_storage_available():
        return storage.platform_store_for_user(user_id)
    raise StoreError(400, "Configure object storage first")


def _is_expired(row: SharedLink) -> bool:
    if not row.expires_at:
        return False
    return auth.utc_aware(row.expires_at) <= datetime.now(timezone.utc)


def _share_out(row: SharedLink | None) -> dict:
    if row is None:
        return {"shared": False, "token": None, "url": None, "expiresAt": None}
    return {
        "shared": True,
        "token": row.token,
        "url": f"/s/{row.token}",
        "expiresAt": auth.utc_aware(row.expires_at).isoformat() if row.expires_at else None,
    }


def _find_link(user_id: str, rel: str, db: Session) -> SharedLink | None:
    return db.scalar(select(SharedLink).where(SharedLink.user_id == user_id, SharedLink.page_path == rel))


@router.get("")
def get_share(path: str, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    provider = active_provider(user.id, db)
    store = store_for_user(user.id, db, provider)
    rel = store.page_rel(path)
    link = _find_link(user.id, rel, db)
    if link and link.provider != provider:
        return _share_out(None)
    if link and _is_expired(link):
        db.delete(link)
        db.commit()
        return _share_out(None)
    return _share_out(link)


@router.post("")
def create_share(body: ShareCreateIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    expires_at = None
    if body.expiresHours is not None:
        if body.expiresHours < 1 or body.expiresHours > 8760:
            raise HTTPException(status_code=400, detail="Invalid expiry hours")
        expires_at = datetime.now(timezone.utc) + timedelta(hours=body.expiresHours)
    provider = active_provider(user.id, db)
    store = store_for_user(user.id, db, provider)
    rel = store.page_rel(body.path)
    if not store.page_exists(rel):
        raise StoreError(404, "Not found")
    link = _find_link(user.id, rel, db)
    if link:
        link.provider = provider
        link.expires_at = expires_at
    else:
        link = SharedLink(
            token=secrets.token_urlsafe(18)[:24],
            user_id=user.id,
            page_path=rel,
            provider=provider,
            expires_at=expires_at,
        )
        db.add(link)
    db.commit()
    db.refresh(link)
    return _share_out(link)


@router.delete("")
def revoke_share(path: str, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    provider = active_provider(user.id, db)
    store = store_for_user(user.id, db, provider)
    rel = store.page_rel(path)
    link = _find_link(user.id, rel, db)
    if link and link.provider == provider:
        db.delete(link)
        db.commit()
    return _share_out(None)


def public_share(token: str, db: Session) -> str | None:
    link = db.get(SharedLink, token)
    if not link:
        return None
    if _is_expired(link):
        db.delete(link)
        db.commit()
        return None
    try:
        store = store_for_user(link.user_id, db, link.provider)
        return store.read_page(link.page_path)
    except (StoreError, FileNotFoundError):
        return None


def sync_renamed_shares(user_id: str, old_path: str, new_path: str, db: Session) -> None:
    old_clean = old_path.strip("/")
    new_clean = new_path.strip("/")
    if not old_clean or old_clean == new_clean:
        return
    provider = active_provider(user_id, db)
    prefix = f"{old_clean}/"
    rows = db.scalars(select(SharedLink).where(SharedLink.user_id == user_id, SharedLink.provider == provider)).all()
    changed = False
    for row in rows:
        if row.page_path == old_clean:
            row.page_path = new_clean
            changed = True
        elif row.page_path.startswith(prefix):
            row.page_path = f"{new_clean}/{row.page_path[len(prefix):]}"
            changed = True
    if changed:
        db.commit()


def delete_removed_shares(user_id: str, removed_path: str, db: Session) -> None:
    clean = removed_path.strip("/")
    if not clean:
        return
    provider = active_provider(user_id, db)
    prefix = f"{clean}/"
    rows = db.scalars(select(SharedLink).where(SharedLink.user_id == user_id, SharedLink.provider == provider)).all()
    changed = False
    for row in rows:
        if row.page_path == clean or row.page_path.startswith(prefix):
            db.delete(row)
            changed = True
    if changed:
        db.commit()
