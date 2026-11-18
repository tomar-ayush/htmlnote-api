import hashlib
import hmac
import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
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
from .models import AuthSession, SignupOtp, User
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
OTP_SECONDS = 10 * 60
OTP_COOLDOWN = 45
MAX_OTP_ATTEMPTS = 5
OTP_CODE = re.compile(r"^\d{6}$")

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


class VerifySignupIn(BaseModel):
    email: str
    code: str


def send_otp_email(email: str, code: str) -> None:
    api_key = os.environ.get("BREVO_API_KEY", "").strip()
    sender_email = os.environ.get("BREVO_SENDER_EMAIL", "").strip()
    sender_name = os.environ.get("BREVO_SENDER_NAME", "HTML Notebook").strip() or "HTML Notebook"
    if not api_key or not sender_email:
        if COOKIE_SECURE:
            raise StoreError(500, "Email service is not configured (set BREVO_API_KEY and BREVO_SENDER_EMAIL)")
        print(f"[DEV OTP] {email}: {code}", flush=True)
        return
    payload = {
        "sender": {"name": sender_name, "email": sender_email},
        "to": [{"email": email}],
        "subject": f"{code} is your HTML Notebook verification code",
        "textContent": f"Your verification code is {code}. It expires in 10 minutes.",
        "htmlContent": (
            "<div style=\"font-family:system-ui,-apple-system,sans-serif;max-width:440px;margin:24px auto;"
            "padding:24px;border:1px solid #e3e6ea;border-radius:12px;color:#1f2329\">"
            "<h2 style=\"margin:0 0 8px;font-size:18px;color:#6b1799\">HTML Notebook</h2>"
            "<p style=\"margin:0 0 16px;font-size:14px;color:#4a5058\">Use this code to verify your email address:</p>"
            f"<div style=\"font-size:28px;font-weight:700;letter-spacing:6px;padding:12px 16px;"
            f"background:#f6effa;color:#6b1799;border-radius:8px;text-align:center\">{code}</div>"
            "<p style=\"margin:16px 0 0;font-size:12px;color:#69727d\">This code expires in 10 minutes.</p></div>"
        ),
    }
    req = urllib.request.Request(
        "https://api.brevo.com/v3/smtp/email",
        data=json.dumps(payload).encode("utf-8"),
        headers={"accept": "application/json", "content-type": "application/json", "api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            if res.status >= 400:
                raise StoreError(502, "Could not send verification email. Check Brevo sender configuration.")
    except (urllib.error.HTTPError, urllib.error.URLError):
        raise StoreError(502, "Could not send verification email. Check Brevo sender configuration.")


def utc_aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


router = APIRouter(prefix="/api/auth")


@router.get("/me")
def me(request: Request, response: Response, db: Session = Depends(get_db)):
    if not request.cookies.get(CSRF_COOKIE):
        issue_csrf(response)
    user = optional_user(request, db)
    return {"user": public(user) if user else None}


@router.post("/signup")
def signup(body: Credentials, db: Session = Depends(get_db)):
    email = clean_email(body.email)
    if not MIN_PASSWORD <= len(body.password) <= MAX_PASSWORD:
        raise StoreError(400, f"Use a password of at least {MIN_PASSWORD} characters")
    if too_many_failures(f"signup:{email}"):
        raise StoreError(429, "Too many requests. Try again in 15 minutes.")
    if db.scalar(select(User.id).where(User.email == email)):
        raise StoreError(409, "An account with this email already exists. Sign in instead.")
    now = datetime.now(timezone.utc)
    pending = db.get(SignupOtp, email)
    if pending and utc_aware(pending.expires_at) > now and (now - utc_aware(pending.sent_at)).total_seconds() < OTP_COOLDOWN:
        raise StoreError(429, "Please wait 45 seconds before requesting a new code.")
    code = f"{secrets.randbelow(1_000_000):06d}"
    otp_hash = hash_token(f"{email}:{code}")
    pw_hash = hash_password(body.password)
    send_otp_email(email, code)
    record_failure(f"signup:{email}")
    db.execute(delete(SignupOtp).where(SignupOtp.expires_at <= func.now()))
    pending = db.get(SignupOtp, email)
    expires_at = now + timedelta(seconds=OTP_SECONDS)
    if pending:
        pending.password_hash = pw_hash
        pending.otp_hash = otp_hash
        pending.attempts = 0
        pending.expires_at = expires_at
        pending.sent_at = now
    else:
        db.add(
            SignupOtp(
                email=email,
                password_hash=pw_hash,
                otp_hash=otp_hash,
                attempts=0,
                expires_at=expires_at,
                sent_at=now,
            )
        )
    db.commit()
    return {"otpRequired": True, "email": email}


@router.post("/verify-signup")
def verify_signup(body: VerifySignupIn, response: Response, db: Session = Depends(get_db)):
    email = clean_email(body.email)
    code = body.code.strip()
    if not OTP_CODE.match(code):
        raise StoreError(400, "Enter the 6-digit verification code")
    pending = db.get(SignupOtp, email)
    now = datetime.now(timezone.utc)
    if not pending or utc_aware(pending.expires_at) <= now:
        if pending:
            db.delete(pending)
            db.commit()
        raise StoreError(400, "Verification code expired or not found. Request a new code.")
    if pending.attempts >= MAX_OTP_ATTEMPTS:
        db.delete(pending)
        db.commit()
        raise StoreError(429, "Too many incorrect attempts. Request a new code.")
    if not hmac.compare_digest(pending.otp_hash, hash_token(f"{email}:{code}")):
        pending.attempts += 1
        db.commit()
        raise StoreError(401, "Incorrect verification code")
    user = User(id=uuid.uuid4().hex, email=email, password_hash=pending.password_hash)
    db.delete(pending)
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
