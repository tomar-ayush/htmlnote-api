import base64
import hashlib
import os
import re

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .auth import COOKIE_SECURE, current_user
from .db import get_db
from .models import User, UserStorage
from .store import Store, StoreError, clean_prefix, validate_endpoint

B2_ENDPOINT = re.compile(r"^https?://s3\.([a-z0-9-]+)\.backblazeb2\.com$", re.IGNORECASE)


def _fernet() -> Fernet:
    raw = os.environ.get("SECRET_KEY", "").strip()
    if not raw:
        if COOKIE_SECURE:
            raise StoreError(500, "Server SECRET_KEY is not configured")
        raw = "htmlnote-local-dev-secret"
    key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
    return Fernet(key)


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise StoreError(400, "Stored secret key cannot be decrypted. Re-enter your Secret Access Key.")


def resolve_region(endpoint_url: str, region: str) -> str:
    clean_region = region.strip()
    if (not clean_region or clean_region == "auto") and endpoint_url:
        m = B2_ENDPOINT.match(endpoint_url)
        if m:
            return m.group(1)
    if not endpoint_url and (not clean_region or clean_region == "auto"):
        return "us-east-1"
    return clean_region or "auto"


def platform_storage_available() -> bool:
    return bool(
        os.environ.get("PLATFORM_S3_BUCKET", "").strip()
        and os.environ.get("PLATFORM_S3_ACCESS_KEY_ID", "").strip()
        and os.environ.get("PLATFORM_S3_SECRET_ACCESS_KEY", "").strip()
    )


def platform_quota_bytes() -> int:
    try:
        mb = int(float(os.environ.get("PLATFORM_FREE_MB", "50").strip() or "50"))
    except ValueError:
        mb = 50
    return max(1, mb) * 1024 * 1024


def platform_store_for_user(user_id: str) -> Store:
    endpoint = os.environ.get("PLATFORM_S3_ENDPOINT_URL", "").strip()
    region = os.environ.get("PLATFORM_S3_REGION", "auto").strip() or "auto"
    bucket = os.environ.get("PLATFORM_S3_BUCKET", "").strip()
    access_key = os.environ.get("PLATFORM_S3_ACCESS_KEY_ID", "").strip()
    secret_key = os.environ.get("PLATFORM_S3_SECRET_ACCESS_KEY", "").strip()
    base = clean_prefix(os.environ.get("PLATFORM_S3_PREFIX", "users"))
    prefix = f"{base}/{user_id}" if base else user_id
    path_style = os.environ.get("PLATFORM_S3_FORCE_PATH_STYLE", "false").lower() == "true"
    return Store(
        endpoint_url=endpoint,
        region=resolve_region(endpoint, region),
        bucket=bucket,
        access_key_id=access_key,
        secret_access_key=secret_key,
        prefix=prefix,
        force_path_style=path_style,
        quota_bytes=platform_quota_bytes(),
    )


class StorageIn(BaseModel):
    endpointUrl: str = ""
    region: str = "auto"
    bucket: str
    accessKeyId: str
    secretAccessKey: str = ""
    prefix: str = ""
    forcePathStyle: bool = False


def public_storage(cfg: UserStorage | None, user_id: str) -> dict:
    if cfg:
        return {
            "configured": True,
            "mode": "custom",
            "usedBytes": None,
            "quotaBytes": None,
            "endpointUrl": cfg.endpoint_url,
            "region": cfg.region,
            "bucket": cfg.bucket,
            "accessKeyId": cfg.access_key_id,
            "hasSecretKey": True,
            "prefix": cfg.prefix,
            "forcePathStyle": cfg.force_path_style,
        }
    if platform_storage_available():
        try:
            used = platform_store_for_user(user_id).used_bytes()
        except StoreError:
            used = 0
        return {
            "configured": True,
            "mode": "managed",
            "usedBytes": used,
            "quotaBytes": platform_quota_bytes(),
            "endpointUrl": "",
            "region": "auto",
            "bucket": "",
            "accessKeyId": "",
            "hasSecretKey": False,
            "prefix": "",
            "forcePathStyle": False,
        }
    return {
        "configured": False,
        "mode": "none",
        "usedBytes": None,
        "quotaBytes": None,
        "endpointUrl": "",
        "region": "auto",
        "bucket": "",
        "accessKeyId": "",
        "hasSecretKey": False,
        "prefix": "",
        "forcePathStyle": False,
    }


def prepare_config(body: StorageIn, existing: UserStorage | None) -> tuple[str, str, str, str, str, str, bool]:
    endpoint_url = validate_endpoint(body.endpointUrl)
    bucket = body.bucket.strip()
    if not bucket or len(bucket) > 255 or "/" in bucket or any(c.isspace() for c in bucket):
        raise StoreError(400, "Enter a valid bucket name")
    access_key_id = body.accessKeyId.strip()
    if not access_key_id or len(access_key_id) > 255:
        raise StoreError(400, "Enter a valid Access Key ID")
    secret_input = body.secretAccessKey.strip()
    if secret_input:
        if len(secret_input) > 512:
            raise StoreError(400, "Secret Access Key is too long")
        secret_access_key = secret_input
    elif existing:
        secret_access_key = decrypt_secret(existing.secret_key_enc)
    else:
        raise StoreError(400, "Enter your Secret Access Key")
    prefix = clean_prefix(body.prefix)
    if len(prefix) > 255:
        raise StoreError(400, "Prefix is too long")
    region = resolve_region(endpoint_url, body.region)
    if len(region) > 64:
        raise StoreError(400, "Region is too long")
    return endpoint_url, region, bucket, access_key_id, secret_access_key, prefix, bool(body.forcePathStyle)


router = APIRouter(prefix="/api/storage")


@router.get("")
def get_storage(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return public_storage(db.get(UserStorage, user.id), user.id)


@router.post("/test")
def test_storage(body: StorageIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    existing = db.get(UserStorage, user.id)
    endpoint_url, region, bucket, access_key_id, secret_access_key, prefix, force_path_style = prepare_config(body, existing)
    Store(
        endpoint_url=endpoint_url,
        region=region,
        bucket=bucket,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        prefix=prefix,
        force_path_style=force_path_style,
    ).verify_access()
    return {"ok": True}


@router.put("")
def save_storage(body: StorageIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    existing = db.get(UserStorage, user.id)
    endpoint_url, region, bucket, access_key_id, secret_access_key, prefix, force_path_style = prepare_config(body, existing)
    Store(
        endpoint_url=endpoint_url,
        region=region,
        bucket=bucket,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        prefix=prefix,
        force_path_style=force_path_style,
    ).verify_access()
    secret_enc = encrypt_secret(secret_access_key)
    if existing:
        existing.endpoint_url = endpoint_url
        existing.region = region
        existing.bucket = bucket
        existing.access_key_id = access_key_id
        existing.secret_key_enc = secret_enc
        existing.prefix = prefix
        existing.force_path_style = force_path_style
        cfg = existing
    else:
        cfg = UserStorage(
            user_id=user.id,
            endpoint_url=endpoint_url,
            region=region,
            bucket=bucket,
            access_key_id=access_key_id,
            secret_key_enc=secret_enc,
            prefix=prefix,
            force_path_style=force_path_style,
        )
        db.add(cfg)
    db.commit()
    return public_storage(cfg, user.id)


@router.delete("")
def delete_storage(user: User = Depends(current_user), db: Session = Depends(get_db)):
    cfg = db.get(UserStorage, user.id)
    if cfg:
        db.delete(cfg)
        db.commit()
    return public_storage(None, user.id)
