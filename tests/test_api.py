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
        self.assertEqual(script.get_heads(), ["0005"])
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
            self.assertEqual((s, ver["html"]), (200, "<head><title>First</title></head>\n<h1>Hello</h1>"))

            s, hdrs, body = await user_a.request("GET", "/page/Work/Notes/First.html")
            self.assertEqual(s, 200)
            self.assertEqual(body, "<h1>Updated</h1>")
            self.assertIn("script-src 'none'", hdrs["content-security-policy"])
            s, hdrs, body = await user_a.request("GET", "/view/Work/Notes/First.html")
            self.assertEqual(s, 200)
            self.assertIn("sandbox", hdrs["content-security-policy"])

            s, _, d = await user_a.request("POST", "/api/rename", {"path": "Work/Notes/First.html", "name": "Renamed"})
            self.assertEqual((s, d), (200, {"path": "Work/Notes/Renamed.html"}))
            s, _, body = await user_a.request("GET", "/page/Work/Notes/Renamed.html")
            self.assertEqual(s, 200)
            self.assertIn("<head><title>Renamed</title></head>", body)
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

    async def test_share_links_and_html_title_sync(self):
        from datetime import datetime, timedelta, timezone

        from app.auth import hash_token
        from app.main import VIEW_CSP
        from app.models import SharedLink, SignupOtp

        alice = AsgiClient(app)
        await alice.request("GET", "/api/auth/me")
        with patch("app.auth.send_otp_email"):
            s, _, _ = await alice.request(
                "POST",
                "/api/auth/signup",
                {"email": "sharer@example.com", "password": "password123"},
            )
            self.assertEqual(s, 200)
        with self.SessionLocal() as db:
            otp = db.get(SignupOtp, "sharer@example.com")
            self.assertIsNotNone(otp)
            otp.otp_hash = hash_token("sharer@example.com:123456")
            db.commit()
        s, _, _ = await alice.request("POST", "/api/auth/verify-signup", {"email": "sharer@example.com", "code": "123456"})
        self.assertEqual(s, 200)

        s, _, _ = await alice.request(
            "PUT",
            "/api/storage",
            {
                "endpointUrl": self.endpoint,
                "region": "us-east-1",
                "bucket": "test-bucket",
                "accessKeyId": "test-access",
                "secretAccessKey": "test-secret",
                "prefix": "share-user",
                "forcePathStyle": True,
            },
        )
        self.assertEqual(s, 200)
        s, _, _ = await alice.request("POST", "/api/folder", {"parent": "", "name": "Work"})
        self.assertEqual(s, 200)
        s, _, _ = await alice.request("POST", "/api/folder", {"parent": "Work", "name": "Notes"})
        self.assertEqual(s, 200)
        s, _, _ = await alice.request(
            "POST",
            "/api/page",
            {"section": "Work/Notes", "name": "Draft", "html": "<!doctype html><html><body><h1>Share Me</h1></body></html>"},
        )
        self.assertEqual(s, 200)

        s, _, body = await alice.request("GET", "/page/Work/Notes/Draft.html")
        self.assertEqual(s, 200)
        self.assertIn("<head><title>Draft</title></head>", body)

        s, _, d = await alice.request("GET", "/api/share?path=Work/Notes/Draft.html")
        self.assertEqual(s, 200)
        self.assertFalse(d["shared"])

        s, _, d = await alice.request("POST", "/api/share", {"path": "Work/Notes/Draft.html", "expiresHours": 24})
        self.assertEqual(s, 200)
        self.assertTrue(d["shared"])
        token = d["token"]
        self.assertEqual(d["url"], f"/s/{token}")
        self.assertIsNotNone(d["expiresAt"])

        anon = AsgiClient(app)
        s, h, body = await anon.request("GET", f"/s/{token}")
        self.assertEqual(s, 200)
        self.assertIn("<h1>Share Me</h1>", body)
        self.assertIn("<title>Draft</title>", body)
        self.assertEqual(h.get("content-security-policy"), VIEW_CSP)

        s, _, d = await alice.request("POST", "/api/share", {"path": "Work/Notes/Draft.html", "expiresHours": None})
        self.assertEqual(s, 200)
        self.assertEqual(d["token"], token)
        self.assertIsNone(d["expiresAt"])

        s, _, r = await alice.request("POST", "/api/rename", {"path": "Work/Notes/Draft.html", "name": "Published"})
        self.assertEqual(s, 200)
        self.assertEqual(r["path"], "Work/Notes/Published.html")

        s, _, d = await alice.request("GET", "/api/share?path=Work/Notes/Published.html")
        self.assertEqual(s, 200)
        self.assertTrue(d["shared"])
        self.assertEqual(d["token"], token)

        s, _, body = await anon.request("GET", f"/s/{token}")
        self.assertEqual(s, 200)
        self.assertIn("<title>Published</title>", body)

        s, _, _ = await alice.request("POST", "/api/share/presign", {"path": "Work/Notes/Published.html", "expiresHours": 24})
        self.assertIn(s, (404, 405))

        s, _, d = await alice.request("DELETE", "/api/share?path=Work/Notes/Published.html")
        self.assertEqual(s, 200)
        self.assertFalse(d["shared"])
        s, _, _ = await anon.request("GET", f"/s/{token}")
        self.assertEqual(s, 404)

        s, _, d = await alice.request("POST", "/api/share", {"path": "Work/Notes/Published.html", "expiresHours": 1})
        self.assertEqual(s, 200)
        exp_token = d["token"]
        with self.SessionLocal() as db:
            row = db.get(SharedLink, exp_token)
            self.assertIsNotNone(row)
            row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=5)
            db.commit()
        s, _, _ = await anon.request("GET", f"/s/{exp_token}")
        self.assertEqual(s, 404)

        for pname in ("Zebra", "Alpha", "Middle"):
            s, _, _ = await alice.request("POST", "/api/page", {"section": "Work/Notes", "name": pname, "html": f"<p>{pname}</p>"})
            self.assertEqual(s, 200)
        s, _, _ = await alice.request("POST", "/api/folder", {"parent": "", "name": "SecondNB"})
        self.assertEqual(s, 200)

        s, _, tree = await alice.request("GET", "/api/tree")
        self.assertEqual(s, 200)
        self.assertEqual([n["name"] for n in tree], ["Work", "SecondNB"])
        self.assertEqual([p["name"] for p in tree[0]["sections"][0]["pages"]], ["Published", "Zebra", "Alpha", "Middle"])

        s, _, _ = await alice.request("POST", "/api/rename", {"path": "Work/Notes/Zebra.html", "name": "Beta"})
        self.assertEqual(s, 200)
        s, _, tree = await alice.request("GET", "/api/tree")
        self.assertEqual([p["name"] for p in tree[0]["sections"][0]["pages"]], ["Published", "Beta", "Alpha", "Middle"])

        s, _, _ = await alice.request(
            "POST",
            "/api/reorder",
            {
                "kind": "pages",
                "parent": "Work/Notes",
                "items": ["Work/Notes/Middle.html", "Work/Notes/Alpha.html", "Work/Notes/Beta.html", "Work/Notes/Published.html"],
            },
        )
        self.assertEqual(s, 200)
        s, _, _ = await alice.request("POST", "/api/reorder", {"kind": "notebooks", "parent": "", "items": ["SecondNB", "Work"]})
        self.assertEqual(s, 200)
        s, _, tree = await alice.request("GET", "/api/tree")
        self.assertEqual([n["name"] for n in tree], ["SecondNB", "Work"])
        self.assertEqual([p["name"] for p in tree[1]["sections"][0]["pages"]], ["Middle", "Alpha", "Beta", "Published"])

    async def test_google_drive_oauth_and_crud_and_provider_switch(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import re
        from urllib.parse import parse_qs, unquote, urlsplit

        from app.auth import hash_token
        from app.models import SignupOtp

        drive_files: dict[str, dict] = {}
        drive_lock = threading.Lock()
        seq = {"n": 0}

        def next_id() -> str:
            seq["n"] += 1
            return f"drv_{seq['n']}"

        def delete_subtree(fid: str) -> None:
            children = [k for k, v in list(drive_files.items()) if fid in v.get("parents", [])]
            for cid in children:
                delete_subtree(cid)
            drive_files.pop(fid, None)

        class FakeGoogleHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format, *args):
                return

            def _send_json(self, status: int, data: dict):
                body = json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _send_bytes(self, status: int, data: bytes, ctype: str = "text/html; charset=utf-8"):
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urlsplit(self.path)
                qs = parse_qs(u.query, keep_blank_values=True)
                if u.path == "/oauth/userinfo":
                    self._send_json(200, {"email": "writer@gmail.com", "verified_email": True})
                    return
                if u.path == "/drive/v3/files":
                    q = qs.get("q", [""])[0]
                    with drive_lock:
                        if "'root' in parents" in q:
                            m = re.search(r"name='([^']+)'", q)
                            target_name = m.group(1) if m else ""
                            matches = [
                                {k: v for k, v in item.items() if k != "data"}
                                for item in drive_files.values()
                                if "root" in item.get("parents", []) and item.get("name") == target_name
                            ]
                            self._send_json(200, {"files": matches})
                            return
                        files = [{k: v for k, v in item.items() if k != "data"} for item in drive_files.values()]
                    self._send_json(200, {"files": files})
                    return
                if u.path.startswith("/drive/v3/files/") and qs.get("alt", [""])[0] == "media":
                    fid = unquote(u.path.split("/")[-1])
                    with drive_lock:
                        item = drive_files.get(fid)
                    if not item:
                        self._send_json(404, {"error": {"message": "Not found"}})
                        return
                    self._send_bytes(200, item.get("data", b""))
                    return
                self._send_json(404, {"error": {"message": "Unknown route"}})

            def do_POST(self):
                u = urlsplit(self.path)
                qs = parse_qs(u.query, keep_blank_values=True)
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b""
                if u.path == "/oauth/token":
                    self._send_json(
                        200,
                        {"access_token": "acc_123", "refresh_token": "ref_123", "expires_in": 3600},
                    )
                    return
                if u.path == "/drive/v3/files":
                    meta = json.loads(raw.decode("utf-8"))
                    with drive_lock:
                        fid = next_id()
                        item = {
                            "id": fid,
                            "name": meta["name"],
                            "mimeType": meta["mimeType"],
                            "parents": meta.get("parents", []),
                            "createdTime": "2026-11-18T10:00:00.000Z",
                            "size": "0",
                        }
                        drive_files[fid] = item
                    self._send_json(200, {"id": fid, "name": item["name"]})
                    return
                if u.path == "/upload/drive/v3/files" and qs.get("uploadType", [""])[0] == "multipart":
                    ctype = self.headers.get("Content-Type", "")
                    m = re.search(r"boundary=([^\s;]+)", ctype)
                    boundary = m.group(1).encode("utf-8") if m else b""
                    sections = raw.split(b"--" + boundary)
                    parts = [s.split(b"\r\n\r\n", 1)[1].rstrip(b"\r\n") for s in sections if b"\r\n\r\n" in s]
                    meta = json.loads(parts[0].decode("utf-8"))
                    content = parts[1]
                    with drive_lock:
                        fid = next_id()
                        drive_files[fid] = {
                            "id": fid,
                            "name": meta["name"],
                            "mimeType": meta.get("mimeType", "text/html"),
                            "parents": meta.get("parents", []),
                            "createdTime": "2026-11-18T10:00:00.000Z",
                            "size": str(len(content)),
                            "data": content,
                        }
                    self._send_json(200, {"id": fid, "name": meta["name"]})
                    return
                self._send_json(404, {"error": {"message": "Unknown POST"}})

            def do_PATCH(self):
                u = urlsplit(self.path)
                qs = parse_qs(u.query, keep_blank_values=True)
                fid = unquote(u.path.split("/")[-1])
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b""
                with drive_lock:
                    item = drive_files.get(fid)
                    if not item:
                        self._send_json(404, {"error": {"message": "Not found"}})
                        return
                    if qs.get("uploadType", [""])[0] == "media":
                        item["data"] = raw
                        item["size"] = str(len(raw))
                        self._send_json(200, {"id": fid, "name": item["name"]})
                        return
                    patch_meta = json.loads(raw.decode("utf-8"))
                    if "name" in patch_meta:
                        item["name"] = patch_meta["name"]
                    self._send_json(200, {"id": fid, "name": item["name"]})

            def do_DELETE(self):
                u = urlsplit(self.path)
                fid = unquote(u.path.split("/")[-1])
                with drive_lock:
                    if fid not in drive_files:
                        self._send_json(404, {"error": {"message": "Not found"}})
                        return
                    delete_subtree(fid)
                self._send_bytes(204, b"", "")

        gserver = ThreadingHTTPServer(("127.0.0.1", 0), FakeGoogleHandler)
        gthread = threading.Thread(target=gserver.serve_forever, daemon=True)
        gthread.start()
        gbase = f"http://127.0.0.1:{gserver.server_port}"
        env_keys = (
            "GOOGLE_CLIENT_ID",
            "GOOGLE_CLIENT_SECRET",
            "GOOGLE_TOKEN_URL",
            "GOOGLE_USERINFO_URL",
            "GOOGLE_DRIVE_API_BASE",
            "GOOGLE_DRIVE_UPLOAD_BASE",
        )
        prev = {k: os.environ.get(k) for k in env_keys}
        os.environ["GOOGLE_CLIENT_ID"] = "test-google-client-id"
        os.environ["GOOGLE_CLIENT_SECRET"] = "test-google-secret"
        os.environ["GOOGLE_TOKEN_URL"] = f"{gbase}/oauth/token"
        os.environ["GOOGLE_USERINFO_URL"] = f"{gbase}/oauth/userinfo"
        os.environ["GOOGLE_DRIVE_API_BASE"] = f"{gbase}/drive/v3"
        os.environ["GOOGLE_DRIVE_UPLOAD_BASE"] = f"{gbase}/upload/drive/v3"
        try:
            user = AsgiClient(app)
            await user.request("GET", "/api/auth/me")
            with patch("app.auth.send_otp_email"):
                s, _, _ = await user.request("POST", "/api/auth/signup", {"email": "drive@example.com", "password": "password123"})
                self.assertEqual(s, 200)
            with self.SessionLocal() as db:
                otp = db.get(SignupOtp, "drive@example.com")
                self.assertIsNotNone(otp)
                otp.otp_hash = hash_token("drive@example.com:112233")
                db.commit()
            s, _, _ = await user.request("POST", "/api/auth/verify-signup", {"email": "drive@example.com", "code": "112233"})
            self.assertEqual(s, 200)

            s, _, cfg = await user.request("GET", "/api/storage")
            self.assertEqual(s, 200)
            self.assertTrue(cfg["driveAvailable"])

            s, _, auth_res = await user.request("GET", "/api/drive/auth-url")
            self.assertEqual(s, 200)
            state = parse_qs(urlsplit(auth_res["url"]).query)["state"][0]

            s, hdrs, _ = await user.request("GET", f"/api/drive/callback?code=mycode&state={state}")
            self.assertEqual(s, 302)
            self.assertEqual(hdrs.get("location"), "/?drive=connected")

            s, _, cfg = await user.request("GET", "/api/storage")
            self.assertEqual(s, 200)
            self.assertEqual(cfg["mode"], "gdrive")
            self.assertTrue(cfg["driveActive"])
            self.assertTrue(cfg["driveConnected"])
            self.assertEqual(cfg["driveEmail"], "writer@gmail.com")
            self.assertEqual(cfg["driveFolderName"], "HTML Notebook")

            s, _, _ = await user.request("POST", "/api/folder", {"parent": "", "name": "DriveNB"})
            self.assertEqual(s, 200)
            s, _, _ = await user.request("POST", "/api/folder", {"parent": "DriveNB", "name": "Ideas"})
            self.assertEqual(s, 200)
            s, _, _ = await user.request(
                "POST",
                "/api/page",
                {
                    "section": "DriveNB/Ideas",
                    "name": "Plan",
                    "html": "<!DOCTYPE html><html><head><title>Old</title></head><body><p>From Drive</p></body></html>",
                },
            )
            self.assertEqual(s, 200)

            s, _, html = await user.request("GET", "/page/DriveNB/Ideas/Plan.html")
            self.assertEqual(s, 200)
            self.assertIn("<title>Plan</title>", html)

            s, _, _ = await user.request(
                "PUT",
                "/api/page?path=DriveNB/Ideas/Plan.html",
                {"html": "<!DOCTYPE html><html><head><title>Plan</title></head><body><p>Updated Drive</p></body></html>", "keepVersion": True},
            )
            self.assertEqual(s, 200)
            s, _, vers = await user.request("GET", "/api/versions?path=DriveNB/Ideas/Plan.html")
            self.assertEqual((s, len(vers)), (200, 1))

            s, _, renamed = await user.request("POST", "/api/rename", {"path": "DriveNB/Ideas/Plan.html", "name": "Final"})
            self.assertEqual((s, renamed), (200, {"path": "DriveNB/Ideas/Final.html"}))
            s, _, html = await user.request("GET", "/page/DriveNB/Ideas/Final.html")
            self.assertEqual(s, 200)
            self.assertIn("<title>Final</title>", html)

            s, _, share_res = await user.request("POST", "/api/share", {"path": "DriveNB/Ideas/Final.html", "expiresHours": 24})
            self.assertEqual(s, 200)
            token = share_res["token"]

            anon = AsgiClient(app)
            s, _, shared_html = await anon.request("GET", f"/s/{token}")
            self.assertEqual(s, 200)
            self.assertIn("<p>Updated Drive</p>", shared_html)

            s, _, _ = await user.request(
                "PUT",
                "/api/storage",
                {
                    "endpointUrl": self.endpoint,
                    "region": "us-east-1",
                    "bucket": "test-bucket",
                    "accessKeyId": "test-access",
                    "secretAccessKey": "test-secret",
                    "prefix": "drive-switch-user",
                    "forcePathStyle": True,
                },
            )
            self.assertEqual(s, 200)
            s, _, s3_tree = await user.request("GET", "/api/tree")
            self.assertEqual((s, s3_tree), (200, []))

            s, _, shared_html_still = await anon.request("GET", f"/s/{token}")
            self.assertEqual(s, 200)
            self.assertIn("<p>Updated Drive</p>", shared_html_still)

            s, _, cfg = await user.request("POST", "/api/drive/provider", {"provider": "gdrive"})
            self.assertEqual((s, cfg["mode"]), (200, "gdrive"))
            s, _, d_tree = await user.request("GET", "/api/tree")
            self.assertEqual(s, 200)
            self.assertEqual(d_tree[0]["sections"][0]["pages"][0]["name"], "Final")

            for pname in ("Zebra", "Alpha"):
                s, _, _ = await user.request("POST", "/api/page", {"section": "DriveNB/Ideas", "name": pname, "html": f"<p>{pname}</p>"})
                self.assertEqual(s, 200)
            s, _, d_tree = await user.request("GET", "/api/tree")
            self.assertEqual([p["name"] for p in d_tree[0]["sections"][0]["pages"]], ["Final", "Zebra", "Alpha"])
            s, _, _ = await user.request(
                "POST",
                "/api/reorder",
                {"kind": "pages", "parent": "DriveNB/Ideas", "items": ["Alpha.html", "Final.html", "Zebra.html"]},
            )
            self.assertEqual(s, 200)
            s, _, d_tree = await user.request("GET", "/api/tree")
            self.assertEqual([p["name"] for p in d_tree[0]["sections"][0]["pages"]], ["Alpha", "Final", "Zebra"])

            s, _, cfg = await user.request("DELETE", "/api/drive")
            self.assertEqual(s, 200)
            self.assertFalse(cfg["driveConnected"])
            self.assertEqual(cfg["mode"], "custom")

            guest = AsgiClient(app)
            await guest.request("GET", "/api/auth/me")
            s, hdrs, _ = await guest.request("GET", "/api/drive/login")
            self.assertEqual(s, 302)
            login_state = parse_qs(urlsplit(hdrs["location"]).query)["state"][0]
            s, hdrs, _ = await AsgiClient(app).request("GET", f"/api/drive/callback?code=mycode&state={login_state}")
            self.assertEqual(s, 302)
            self.assertIn("drive_error", hdrs.get("location", ""))
            s, hdrs, _ = await guest.request("GET", f"/api/drive/callback?code=mycode&state={login_state}")
            self.assertEqual((s, hdrs.get("location")), (302, "/?drive=connected"))
            s, _, me = await guest.request("GET", "/api/auth/me")
            self.assertEqual(me["user"]["email"], "writer@gmail.com")
            s, _, cfg = await guest.request("GET", "/api/storage")
            self.assertEqual((cfg["mode"], cfg["driveEmail"]), ("gdrive", "writer@gmail.com"))
        finally:
            gserver.shutdown()
            gserver.server_close()
            for k, v in prev.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()
