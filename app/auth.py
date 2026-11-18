import hashlib
import hmac
import os
import re
import secrets
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from threading import Lock

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .db import get_db
from .models import AuthSession, User
from .store import StoreError

COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "true").lower() != "false"
COOKIE_PREFIX = "__Host-" if COOKIE_SECURE else ""
SESSION_COOKIE = f"{COOKIE_PREFIX}hn_session"
CSRF_COOKIE = f"{COOKIE_PREFIX}hn_csrf"
CSRF_HEADER = "x-csrf-token"
SESSION_SECONDS = 30 * 24 * 60 * 60
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD, MAX_PASSWORD = 8, 256
MAX_FAILURES = 10
FAILURE_WINDOW = 15 * 60

_failures: dict[str, deque[float]] = defaultdict(deque)
_failures_lock = Lock()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=32)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split("$")
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    except ValueError:
        return False
    return hmac.compare_digest(actual.hex(), digest)


DUMMY_HASH = hash_password(secrets.token_hex(16))


def too_many_failures(key: str) -> bool:
    now = time.monotonic()
    with _failures_lock:
        attempts = _failures.get(key)
        if not attempts:
            return False
        while attempts and now - attempts[0] > FAILURE_WINDOW:
            attempts.popleft()
        if not attempts:
            del _failures[key]
        return len(attempts) >= MAX_FAILURES


def record_failure(key: str) -> None:
    with _failures_lock:
        _failures[key].append(time.monotonic())


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def set_cookie(response: Response, name: str, value: str, http_only: bool) -> None:
    response.set_cookie(
        name,
        value,
        max_age=SESSION_SECONDS,
        path="/",
        secure=COOKIE_SECURE,
        httponly=http_only,
        samesite="lax",
    )


def issue_csrf(response: Response) -> None:
    set_cookie(response, CSRF_COOKIE, secrets.token_urlsafe(32), http_only=False)


def csrf_valid(request: Request) -> bool:
    cookie = request.cookies.get(CSRF_COOKIE, "")
    header = request.headers.get(CSRF_HEADER, "")
    return bool(cookie) and hmac.compare_digest(cookie, header)


def start_session(db: Session, response: Response, user: User) -> None:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(timezone.utc) + timedelta(seconds=SESSION_SECONDS)
    db.execute(delete(AuthSession).where(AuthSession.user_id == user.id, AuthSession.expires_at <= func.now()))
    db.add(AuthSession(token_hash=hash_token(token), user_id=user.id, expires_at=expires))
    db.commit()
    set_cookie(response, SESSION_COOKIE, token, http_only=True)
    issue_csrf(response)


def optional_user(request: Request, db: Session) -> User | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return db.scalar(
        select(User)
        .join(AuthSession, AuthSession.user_id == User.id)
        .where(AuthSession.token_hash == hash_token(token), AuthSession.expires_at > func.now())
    )


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = optional_user(request, db)
    if not user:
        raise StoreError(401, "Sign in to continue")
    return user


def public(user: User) -> dict:
    return {"id": user.id, "email": user.email}


def clean_email(email: str) -> str:
    value = email.strip().lower()
    if len(value) > 254 or not EMAIL.match(value):
        raise StoreError(400, "Enter a valid email address")
    return value


class Credentials(BaseModel):
    email: str
    password: str


router = APIRouter(prefix="/api/auth")


@router.get("/me")
def me(request: Request, response: Response, db: Session = Depends(get_db)):
    if not request.cookies.get(CSRF_COOKIE):
        issue_csrf(response)
    user = optional_user(request, db)
    return {"user": public(user) if user else None}


@router.post("/signup")
def signup(body: Credentials, response: Response, db: Session = Depends(get_db)):
    email = clean_email(body.email)
    if not MIN_PASSWORD <= len(body.password) <= MAX_PASSWORD:
        raise StoreError(400, f"Use a password of at least {MIN_PASSWORD} characters")
    if db.scalar(select(User.id).where(User.email == email)):
        raise StoreError(409, "An account with this email already exists. Sign in instead.")
    user = User(id=uuid.uuid4().hex, email=email, password_hash=hash_password(body.password))
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise StoreError(409, "An account with this email already exists. Sign in instead.")
    start_session(db, response, user)
    return {"user": public(user)}


@router.post("/login")
def login(body: Credentials, response: Response, db: Session = Depends(get_db)):
    email = body.email.strip().lower()
    if too_many_failures(email):
        raise StoreError(429, "Too many failed attempts. Try again in 15 minutes.")
    user = db.scalar(select(User).where(User.email == email))
    valid = verify_password(body.password[:MAX_PASSWORD], user.password_hash if user else DUMMY_HASH)
    if not user or not valid:
        record_failure(email)
        raise StoreError(401, "Email or password is incorrect")
    start_session(db, response, user)
    return {"user": public(user)}


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db)):
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == hash_token(token)))
        db.commit()
    response.delete_cookie(SESSION_COOKIE, path="/", secure=COOKIE_SECURE, httponly=True, samesite="lax")
    issue_csrf(response)
    return {"user": None}
