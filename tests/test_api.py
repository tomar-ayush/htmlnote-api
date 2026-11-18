import email.utils
import http.cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, unquote, urlsplit
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

os.environ["COOKIE_SECURE"] = "false"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["ALLOW_INSECURE_S3"] = "true"
os.environ["ALLOWED_ORIGINS"] = "https://htmlnote-web.vercel.app"

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import auth as auth_mod
from app.db import get_db, normalize_db_url
from app.main import app
from app.models import Base, UserStorage
from app.storage import resolve_region
from app.store import StoreError, validate_endpoint

BUCKETS: dict[str, dict[str, tuple[bytes, float, str]]] = {
    "test-bucket": {},
    "other-bucket": {},
}
LOCK = threading.Lock()


class S3Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):
        return

    def _send(self, status: int, body: bytes = b"", content_type: str = "application/xml", headers: dict | None = None):
        self.send_response(status)
        hdrs = dict(headers or {})
        hdrs.setdefault("Content-Length", str(len(body)))
        if content_type:
            hdrs.setdefault("Content-Type", content_type)
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.end_headers()
        if body and self.command != "HEAD":
            self.wfile.write(body)

    def _error(self, status: int, code: str, message: str):
        xml = f"<Error><Code>{escape(code)}</Code><Message>{escape(message)}</Message></Error>".encode("utf-8")
        self._send(status, xml)

    def _parse(self):
        u = urlsplit(self.path)
        parts = unquote(u.path).lstrip("/").split("/", 1)
        bucket = parts[0] if parts and parts[0] else ""
        key = parts[1] if len(parts) > 1 else ""
        qs = parse_qs(u.query, keep_blank_values=True)
        return bucket, key, qs

    def do_HEAD(self):
        bucket, key, _ = self._parse()
        with LOCK:
            if bucket not in BUCKETS:
                self._send(404, b"", "")
                return
            item = BUCKETS[bucket].get(key)
            if item is None:
                self._send(404, b"", "")
                return
            data, ts, ct = item
        self._send(
            200,
            b"",
            ct,
            {
                "ETag": '"deadbeef"',
                "Last-Modified": email.utils.formatdate(ts, usegmt=True),
                "Content-Length": str(len(data)),
            },
        )

    def do_GET(self):
        bucket, key, qs = self._parse()
        with LOCK:
            if bucket not in BUCKETS:
                self._error(404, "NoSuchBucket", "Bucket does not exist")
                return
            store = BUCKETS[bucket]
            if "list-type" in qs:
                prefix = qs.get("prefix", [""])[0]
                delimiter = qs.get("delimiter", [""])[0]
                max_keys = int(qs.get("max-keys", ["1000"])[0])
                token = qs.get("continuation-token", [""])[0]
                keys = sorted(k for k in store if k.startswith(prefix))
                common: list[str] = []
                contents: list[tuple[str, bytes, float]] = []
                seen_prefixes: set[str] = set()
                for k in keys:
                    if delimiter:
                        rest = k[len(prefix) :]
                        idx = rest.find(delimiter)
                        if idx != -1:
                            cp = prefix + rest[: idx + len(delimiter)]
                            if cp not in seen_prefixes:
                                seen_prefixes.add(cp)
                                common.append(cp)
                            continue
                    data, ts, _ = store[k]
                    contents.append((k, data, ts))
                start = int(token) if token else 0
                sliced = contents[start : start + max_keys]
                truncated = start + max_keys < len(contents)
                lines = [
                    '<?xml version="1.0" encoding="UTF-8"?>',
                    '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">',
                    f"<Name>{escape(bucket)}</Name>",
                    f"<Prefix>{escape(prefix)}</Prefix>",
                    f"<KeyCount>{len(sliced) + len(common)}</KeyCount>",
                    f"<MaxKeys>{max_keys}</MaxKeys>",
                    f"<IsTruncated>{'true' if truncated else 'false'}</IsTruncated>",
                ]
                if truncated:
                    lines.append(f"<NextContinuationToken>{start + max_keys}</NextContinuationToken>")
                for k, data, ts in sliced:
                    iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(ts))
                    lines.append(
                        f"<Contents><Key>{escape(k)}</Key><LastModified>{iso}</LastModified>"
                        f"<ETag>&quot;deadbeef&quot;</ETag><Size>{len(data)}</Size></Contents>"
                    )
                for cp in common:
                    lines.append(f"<CommonPrefixes><Prefix>{escape(cp)}</Prefix></CommonPrefixes>")
                lines.append("</ListBucketResult>")
                self._send(200, "".join(lines).encode("utf-8"))
                return
            item = store.get(key)
            if item is None:
                self._error(404, "NoSuchKey", "Not found")
                return
            data, ts, ct = item
        self._send(200, data, ct, {"ETag": '"deadbeef"', "Last-Modified": email.utils.formatdate(ts, usegmt=True)})

    def do_PUT(self):
        bucket, key, _ = self._parse()
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        copy_source = self.headers.get("x-amz-copy-source")
        with LOCK:
            if bucket not in BUCKETS:
                self._error(404, "NoSuchBucket", "Bucket does not exist")
                return
            if copy_source:
                src_raw = unquote(copy_source).lstrip("/")
                src_bucket, src_key = src_raw.split("/", 1)
                src_item = BUCKETS.get(src_bucket, {}).get(src_key)
                if src_item is None:
                    self._error(404, "NoSuchKey", "Source not found")
                    return
                data, _, ct = src_item
                BUCKETS[bucket][key] = (data, time.time(), ct)
                iso = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
                xml = f"<CopyObjectResult><LastModified>{iso}</LastModified><ETag>&quot;deadbeef&quot;</ETag></CopyObjectResult>".encode("utf-8")
                self._send(200, xml)
                return
            ct = self.headers.get("Content-Type", "application/octet-stream")
            BUCKETS[bucket][key] = (body, time.time(), ct)
        self._send(200, b"", "text/plain", {"ETag": '"deadbeef"'})

    def do_DELETE(self):
        bucket, key, _ = self._parse()
        with LOCK:
            if bucket not in BUCKETS:
                self._error(404, "NoSuchBucket", "Bucket does not exist")
                return
            BUCKETS[bucket].pop(key, None)
        self._send(204, b"", "")

    def do_POST(self):
        bucket, _, qs = self._parse()
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        with LOCK:
            if bucket not in BUCKETS:
                self._error(404, "NoSuchBucket", "Bucket does not exist")
                return
            if "delete" in qs:
                root = ET.fromstring(body)
                for obj in root.iter():
                    if obj.tag.endswith("Key") and obj.text:
                        BUCKETS[bucket].pop(obj.text, None)
                xml = '<DeleteResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"></DeleteResult>'.encode("utf-8")
                self._send(200, xml)
                return
        self._send(400, b"")


class AsgiClient:
    def __init__(self, asgi_app):
        self.app = asgi_app
        self.cookies: dict[str, str] = {}

    async def request(self, method: str, target: str, json_body=None, origin: str = "http://localhost"):
        path, _, query = target.partition("?")
        req_headers: list[tuple[bytes, bytes]] = [(b"host", b"localhost")]
        if self.cookies:
            cookie_val = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            req_headers.append((b"cookie", cookie_val.encode("latin-1")))
        if method in ("POST", "PUT", "PATCH", "DELETE"):
            if origin:
                req_headers.append((b"origin", origin.encode("latin-1")))
            req_headers.append((b"x-csrf-token", self.cookies.get("hn_csrf", "").encode("latin-1")))
        body = b""
        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            req_headers.append((b"content-type", b"application/json"))
            req_headers.append((b"content-length", str(len(body)).encode("latin-1")))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": unquote(path),
            "raw_path": path.encode("ascii"),
            "query_string": query.encode("ascii"),
            "headers": req_headers,
            "client": ("127.0.0.1", 12345),
            "server": ("localhost", 8000),
        }
        sent = False
        status_code = 500
        resp_headers: dict[str, str] = {}
        chunks: list[bytes] = []

        async def receive():
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                for k, v in message["headers"]:
                    kl = k.decode("latin-1").lower()
                    vl = v.decode("latin-1")
                    if kl == "set-cookie":
                        c = http.cookies.SimpleCookie()
                        c.load(vl)
                        for name, morsel in c.items():
                            if morsel["max-age"] == "0" or morsel.value == "":
                                self.cookies.pop(name, None)
                            else:
                                self.cookies[name] = morsel.value
                    else:
                        resp_headers[kl] = vl
            elif message["type"] == "http.response.body":
                chunks.append(message.get("body", b""))

        await self.app(scope, receive, send)
        raw = b"".join(chunks).decode("utf-8")
        try:
            data = json.loads(raw)
        except Exception:
            data = raw
        return status_code, resp_headers, data


class HtmlNoteApiTests(unittest.IsolatedAsyncioTestCase):
    server: ThreadingHTTPServer
    endpoint: str

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), S3Handler)
        port = cls.server.server_address[1]
        cls.endpoint = f"http://127.0.0.1:{port}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        with LOCK:
            BUCKETS["test-bucket"].clear()
            BUCKETS["other-bucket"].clear()
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.SessionLocal = sessionmaker(self.engine, expire_on_commit=False)

        def override_get_db():
            with self.SessionLocal() as db:
                yield db

        app.dependency_overrides[get_db] = override_get_db

    def tearDown(self):
        app.dependency_overrides.clear()
        self.engine.dispose()

    def test_db_url_and_alembic_and_b2_region(self):
        self.assertEqual(
            normalize_db_url("postgres://u:p@ep-x.neon.tech/db?sslmode=require"),
            "postgresql+psycopg://u:p@ep-x.neon.tech/db?sslmode=require",
        )
        self.assertEqual(
            normalize_db_url("postgresql://u:p@ep-x.neon.tech/db"),
            "postgresql+psycopg://u:p@ep-x.neon.tech/db",
        )
        self.assertEqual(
            normalize_db_url("postgresql+psycopg://u:p@host/db"),
            "postgresql+psycopg://u:p@host/db",
        )
        alembic_cfg = AlembicConfig(str(BACKEND_DIR / "alembic.ini"))
        alembic_cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
        script = ScriptDirectory.from_config(alembic_cfg)
        self.assertEqual(script.get_heads(), ["0003"])
        self.assertEqual(resolve_region("https://s3.us-west-004.backblazeb2.com", "auto"), "us-west-004")
        self.assertEqual(resolve_region("", "auto"), "us-east-1")

    def test_brevo_otp_email(self):
        captured = {}

        class FakeResponse:
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        def fake_urlopen(req, timeout=0):
            captured["url"] = req.full_url
            captured["headers"] = {k.lower(): v for k, v in req.header_items()}
            captured["body"] = json.loads(req.data.decode("utf-8"))
            captured["timeout"] = timeout
            return FakeResponse()

        prev = {k: os.environ.get(k) for k in ("BREVO_API_KEY", "BREVO_SENDER_EMAIL", "BREVO_SENDER_NAME")}
        os.environ["BREVO_API_KEY"] = "xkeysib-test-key"
        os.environ["BREVO_SENDER_EMAIL"] = "noreply@example.com"
        os.environ["BREVO_SENDER_NAME"] = "HTML Notebook"
        try:
            with patch("urllib.request.urlopen", side_effect=fake_urlopen):
                auth_mod.send_otp_email("reader@example.com", "482910")
        finally:
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

        self.assertEqual(captured["url"], "https://api.brevo.com/v3/smtp/email")
        self.assertEqual(captured["headers"]["api-key"], "xkeysib-test-key")
        self.assertEqual(captured["body"]["sender"], {"name": "HTML Notebook", "email": "noreply@example.com"})
        self.assertEqual(captured["body"]["to"], [{"email": "reader@example.com"}])
        self.assertIn("482910", captured["body"]["subject"])
        self.assertIn("482910", captured["body"]["htmlContent"])
        self.assertIn("482910", captured["body"]["textContent"])

    def test_ssrf_validation(self):
        os.environ["ALLOW_INSECURE_S3"] = "false"
        try:
            for bad in (
                "http://127.0.0.1:9000",
                "https://127.0.0.1:9000",
                "https://169.254.169.254",
                "https://bucket.s3.amazonaws.com/path",
            ):
                with self.assertRaises(StoreError) as ctx:
                    validate_endpoint(bad)
                self.assertEqual(ctx.exception.status, 400)
        finally:
            os.environ["ALLOW_INSECURE_S3"] = "true"

    async def test_multi_tenant_s3_crud_and_allowed_origins(self):
        sent_otps: dict[str, str] = {}

        def capture_otp(email: str, code: str) -> None:
            sent_otps[email] = code

        with patch("app.auth.send_otp_email", side_effect=capture_otp):
            user_a = AsgiClient(app)
            s, _, d = await user_a.request("GET", "/api/auth/me")
            self.assertEqual(s, 200)
            self.assertEqual(d, {"user": None})

            s, _, d = await user_a.request(
                "POST",
                "/api/auth/signup",
                {"email": "a@example.com", "password": "password123"},
                origin="https://evil.example.com",
            )
            self.assertEqual(s, 403)

            s, _, d = await user_a.request(
                "POST",
                "/api/auth/signup",
                {"email": "a@example.com", "password": "password123"},
                origin="https://htmlnote-web.vercel.app",
            )
            self.assertEqual((s, d), (200, {"otpRequired": True, "email": "a@example.com"}))
            code_a = sent_otps["a@example.com"]
            bad_code = "000000" if code_a != "000000" else "111111"

            s, _, _ = await user_a.request(
                "POST",
                "/api/auth/verify-signup",
                {"email": "a@example.com", "code": bad_code},
                origin="https://htmlnote-web.vercel.app",
            )
            self.assertEqual(s, 401)

            s, _, d = await user_a.request(
                "POST",
                "/api/auth/verify-signup",
                {"email": "a@example.com", "code": code_a},
                origin="https://htmlnote-web.vercel.app",
            )
            self.assertEqual(s, 200)
            user_a_id = d["user"]["id"]

            s, _, d = await user_a.request("GET", "/api/storage")
            self.assertEqual(s, 200)
            self.assertFalse(d["configured"])
            self.assertEqual(d["mode"], "none")
            s, _, _ = await user_a.request("GET", "/api/tree")
            self.assertEqual(s, 400)

            bad_payload = {
                "endpointUrl": self.endpoint,
                "region": "auto",
                "bucket": "missing-bucket",
                "accessKeyId": "AKIA",
                "secretAccessKey": "SECRET_A",
                "prefix": "user-a",
                "forcePathStyle": True,
            }
            s, _, _ = await user_a.request("POST", "/api/storage/test", bad_payload)
            self.assertEqual(s, 400)

            payload_a = {
                "endpointUrl": self.endpoint,
                "region": "auto",
                "bucket": "test-bucket",
                "accessKeyId": "AKIA_A",
                "secretAccessKey": "SECRET_RAW_A",
                "prefix": "user-a",
                "forcePathStyle": True,
            }
            s, _, d = await user_a.request("POST", "/api/storage/test", payload_a)
            self.assertEqual((s, d), (200, {"ok": True}))
            s, _, d = await user_a.request("PUT", "/api/storage", payload_a)
            self.assertEqual(s, 200)
            self.assertTrue(d["configured"])
            self.assertEqual(d["mode"], "custom")
            self.assertTrue(d["hasSecretKey"])
            self.assertNotIn("secretAccessKey", d)

            with self.SessionLocal() as db:
                row = db.get(UserStorage, user_a_id)
                self.assertIsNotNone(row)
                self.assertNotEqual(row.secret_key_enc, "SECRET_RAW_A")

            s, _, d = await user_a.request("PUT", "/api/storage", {**payload_a, "secretAccessKey": "", "prefix": "tenant-a"})
            self.assertEqual(s, 200)
            self.assertEqual(d["prefix"], "tenant-a")

            s, _, nb = await user_a.request("POST", "/api/folder", {"parent": "", "name": "Work"})
            self.assertEqual((s, nb), (200, {"path": "Work"}))
            s, _, sec = await user_a.request("POST", "/api/folder", {"parent": "Work", "name": "Notes"})
            self.assertEqual((s, sec), (200, {"path": "Work/Notes"}))
            s, _, pg = await user_a.request("POST", "/api/page", {"section": "Work/Notes", "name": "First", "html": "<h1>Hello</h1>"})
            self.assertEqual((s, pg), (200, {"path": "Work/Notes/First.html"}))

            s, _, d = await user_a.request("PUT", "/api/page?path=Work/Notes/First.html", {"html": "<h1>Updated</h1>", "keepVersion": True})
            self.assertEqual((s, d), (200, {"saved": True}))
            s, _, d = await user_a.request("PUT", "/api/page?path=Work/Notes/First.html", {"html": "<h1>Updated Fast</h1>", "keepVersion": False})
            self.assertEqual((s, d), (200, {"saved": True}))
            s, _, d = await user_a.request("PUT", "/api/page?path=Work/Notes/First.html", {"html": "<h1>Updated</h1>", "keepVersion": False})
            self.assertEqual((s, d), (200, {"saved": True}))
            s, _, vers = await user_a.request("GET", "/api/versions?path=Work/Notes/First.html")
            self.assertEqual(s, 200)
            self.assertEqual(len(vers), 1)
            s, _, ver = await user_a.request("GET", f"/api/version?path=Work/Notes/First.html&id={vers[0]['id']}")
            self.assertEqual((s, ver["html"]), (200, "<h1>Hello</h1>"))

            s, hdrs, body = await user_a.request("GET", "/page/Work/Notes/First.html")
            self.assertEqual(s, 200)
            self.assertEqual(body, "<h1>Updated</h1>")
            self.assertIn("script-src 'none'", hdrs["content-security-policy"])
            s, hdrs, body = await user_a.request("GET", "/view/Work/Notes/First.html")
            self.assertEqual(s, 200)
            self.assertIn("sandbox", hdrs["content-security-policy"])

            s, _, d = await user_a.request("POST", "/api/rename", {"path": "Work/Notes/First.html", "name": "Renamed"})
            self.assertEqual((s, d), (200, {"path": "Work/Notes/Renamed.html"}))
            s, _, vers = await user_a.request("GET", "/api/versions?path=Work/Notes/Renamed.html")
            self.assertEqual((s, len(vers)), (200, 1))
            s, _, d = await user_a.request("POST", "/api/rename", {"path": "Work/Notes", "name": "Docs"})
            self.assertEqual((s, d), (200, {"path": "Work/Docs"}))
            s, _, d = await user_a.request("POST", "/api/rename", {"path": "Work", "name": "Projects"})
            self.assertEqual((s, d), (200, {"path": "Projects"}))

            with LOCK:
                BUCKETS["test-bucket"]["tenant-a/AgentNB/Sec/note.html"] = (b"<h1>From Agent</h1>", time.time() + 2, "text/html")

            s, _, tree = await user_a.request("GET", "/api/tree")
            self.assertEqual(s, 200)
            self.assertEqual([n["name"] for n in tree], ["Projects", "AgentNB"])
            self.assertEqual(tree[1]["sections"][0]["pages"], [{"name": "note", "path": "AgentNB/Sec/note.html"}])

            s, _, _ = await user_a.request("DELETE", "/api/item?path=AgentNB/Sec/note.html")
            self.assertEqual(s, 200)
            s, _, tree = await user_a.request("GET", "/api/tree")
            agent_nb = next((n for n in tree if n["name"] == "AgentNB"), None)
            self.assertEqual(s, 200)
            self.assertIsNotNone(agent_nb)
            self.assertEqual(agent_nb["sections"][0], {"name": "Sec", "path": "AgentNB/Sec", "pages": []})

            user_b = AsgiClient(app)
            await user_b.request("GET", "/api/auth/me")
            s, _, _ = await user_b.request("POST", "/api/auth/signup", {"email": "b@example.com", "password": "password123"})
            self.assertEqual(s, 200)
            s, _, _ = await user_b.request("POST", "/api/auth/verify-signup", {"email": "b@example.com", "code": sent_otps["b@example.com"]})
            self.assertEqual(s, 200)
            s, _, d = await user_b.request("GET", "/api/storage")
            self.assertFalse(d["configured"])
            s, _, _ = await user_b.request("PUT", "/api/storage", {**payload_a, "bucket": "other-bucket", "prefix": "tenant-b", "secretAccessKey": "SECRET_B"})
            self.assertEqual(s, 200)
            s, _, tree_b = await user_b.request("GET", "/api/tree")
            self.assertEqual((s, tree_b), (200, []))

            s, _, d = await user_a.request("DELETE", "/api/storage")
            self.assertEqual(s, 200)
            self.assertFalse(d["configured"])
            self.assertEqual(d["mode"], "none")
            s, _, _ = await user_a.request("GET", "/api/tree")
            self.assertEqual(s, 400)

    async def test_managed_shared_r2_tier_and_quota(self):
        env_keys = (
            "PLATFORM_S3_ENDPOINT_URL",
            "PLATFORM_S3_REGION",
            "PLATFORM_S3_BUCKET",
            "PLATFORM_S3_ACCESS_KEY_ID",
            "PLATFORM_S3_SECRET_ACCESS_KEY",
            "PLATFORM_S3_PREFIX",
            "PLATFORM_S3_FORCE_PATH_STYLE",
            "PLATFORM_FREE_MB",
        )
        prev = {k: os.environ.get(k) for k in env_keys}
        os.environ["PLATFORM_S3_ENDPOINT_URL"] = self.endpoint
        os.environ["PLATFORM_S3_REGION"] = "auto"
        os.environ["PLATFORM_S3_BUCKET"] = "test-bucket"
        os.environ["PLATFORM_S3_ACCESS_KEY_ID"] = "PLAT"
        os.environ["PLATFORM_S3_SECRET_ACCESS_KEY"] = "PLAT_SEC"
        os.environ["PLATFORM_S3_PREFIX"] = "managed"
        os.environ["PLATFORM_S3_FORCE_PATH_STYLE"] = "true"
        os.environ["PLATFORM_FREE_MB"] = "1"
        try:
            with patch("app.auth.send_otp_email"):
                user = AsgiClient(app)
                await user.request("GET", "/api/auth/me")
                s, _, d = await user.request("POST", "/api/auth/signup", {"email": "free@example.com", "password": "password123"})
                self.assertEqual(s, 200)
            with self.SessionLocal() as db:
                from app.models import SignupOtp
                otp = db.get(SignupOtp, "free@example.com")
                self.assertIsNotNone(otp)
                otp.otp_hash = auth_mod.hash_token("free@example.com:654321")
                db.commit()
            s, _, d = await user.request("POST", "/api/auth/verify-signup", {"email": "free@example.com", "code": "654321"})
            self.assertEqual(s, 200)
            uid = d["user"]["id"]

            s, _, d = await user.request("GET", "/api/storage")
            self.assertEqual(s, 200)
            self.assertTrue(d["configured"])
            self.assertEqual(d["mode"], "managed")
            self.assertEqual(d["quotaBytes"], 1024 * 1024)

            s, _, _ = await user.request("POST", "/api/folder", {"parent": "", "name": "Starter"})
            self.assertEqual(s, 200)
            s, _, _ = await user.request("POST", "/api/folder", {"parent": "Starter", "name": "Notes"})
            self.assertEqual(s, 200)
            s, _, _ = await user.request("POST", "/api/page", {"section": "Starter/Notes", "name": "Ok", "html": "<p>hi</p>"})
            self.assertEqual(s, 200)
            with LOCK:
                self.assertIn(f"managed/{uid}/Starter/Notes/Ok.html", BUCKETS["test-bucket"])

            oversize = "<p>" + ("x" * (1024 * 1024 + 64)) + "</p>"
            s, _, _ = await user.request("POST", "/api/page", {"section": "Starter/Notes", "name": "TooBig", "html": oversize})
            self.assertEqual(s, 413)
        finally:
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
