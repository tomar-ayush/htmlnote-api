from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import auth, storage
from .db import get_db
from .models import User, UserGoogleDrive
from .store import (
    BAD_CHARS,
    MAX_VERSIONS,
    PAGE_EXT,
    VERSION_GAP_MS,
    VERSION_ID,
    StoreError,
    apply_order,
    empty_order,
    order_append_folder,
    order_append_page,
    order_remove,
    order_rename,
    order_set,
    parse_order,
    sync_html_title,
)

FOLDER_MIME = "application/vnd.google-apps.folder"
ROOT_FOLDER_NAME = "HTML Notebook"
DRIVE_SCOPES = "https://www.googleapis.com/auth/drive.file https://www.googleapis.com/auth/userinfo.email"
_SNAP_LOCK = threading.Lock()
_LAST_DRIVE_SNAPSHOT: OrderedDict[tuple[str, str], int] = OrderedDict()

router = APIRouter(prefix="/api/drive", tags=["drive"])
OAUTH_LOGIN_COOKIE = f"{auth.COOKIE_PREFIX}hn_oauth"


class ProviderIn(BaseModel):
    provider: str


def google_client_id() -> str:
    return os.environ.get("GOOGLE_CLIENT_ID", "").strip()


def google_client_secret() -> str:
    return os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()


def drive_oauth_available() -> bool:
    return bool(google_client_id() and google_client_secret())


def _auth_url() -> str:
    return os.environ.get("GOOGLE_AUTH_URL", "https://accounts.google.com/o/oauth2/v2/auth")


def _token_url() -> str:
    return os.environ.get("GOOGLE_TOKEN_URL", "https://oauth2.googleapis.com/token")


def _userinfo_url() -> str:
    return os.environ.get("GOOGLE_USERINFO_URL", "https://www.googleapis.com/oauth2/v2/userinfo")


def _drive_api_base() -> str:
    return os.environ.get("GOOGLE_DRIVE_API_BASE", "https://www.googleapis.com/drive/v3").rstrip("/")


def _drive_upload_base() -> str:
    return os.environ.get("GOOGLE_DRIVE_UPLOAD_BASE", "https://www.googleapis.com/upload/drive/v3").rstrip("/")


def resolve_redirect_uri(request: Request) -> str:
    configured = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
    if configured:
        return configured
    proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip() or request.url.scheme or "https"
    host = (
        request.headers.get("x-forwarded-host", "").split(",")[0].strip()
        or request.headers.get("host", "").strip()
        or request.url.netloc
    )
    return f"{proto}://{host}/api/drive/callback"


def make_oauth_state(user_id: str) -> str:
    raw = json.dumps({"uid": user_id, "nonce": secrets.token_hex(8)}).encode("utf-8")
    return storage._fernet().encrypt(raw).decode("ascii")


def verify_oauth_state(state: str, expected_user_id: str) -> bool:
    if not state:
        return False
    try:
        raw = storage._fernet().decrypt(state.encode("ascii"), ttl=600)
        data = json.loads(raw.decode("utf-8"))
        return data.get("uid") == expected_user_id
    except (InvalidToken, ValueError, TypeError):
        return False


def _request_raw(method: str, url: str, headers: dict[str, str] | None = None, data: bytes | None = None) -> bytes:
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read()
    except urllib.error.HTTPError as err:
        if err.code == 404:
            raise FileNotFoundError()
        if err.code in (401, 403):
            raise StoreError(403, "Google Drive access denied or expired. Reconnect Google Drive in Storage.")
        detail = ""
        try:
            body = json.loads(err.read().decode("utf-8", errors="ignore"))
            detail = body.get("error_description") or body.get("error", {}).get("message") or ""
        except Exception:
            detail = ""
        raise StoreError(400, detail or f"Google Drive request failed ({err.code})")
    except (urllib.error.URLError, TimeoutError):
        raise StoreError(502, "Cannot connect to Google Drive")


def _request_json(method: str, url: str, headers: dict[str, str] | None = None, data: bytes | None = None) -> dict:
    raw = _request_raw(method, url, headers=headers, data=data)
    if not raw:
        return {}
    return json.loads(raw.decode("utf-8"))


def _exchange_code(code: str, redirect_uri: str) -> dict:
    body = urllib.parse.urlencode(
        {
            "client_id": google_client_id(),
            "client_secret": google_client_secret(),
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    ).encode("utf-8")
    return _request_json(
        "POST",
        _token_url(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=body,
    )


def _refresh_access_token(cfg: UserGoogleDrive, db: Session) -> str:
    refresh_token = storage.decrypt_secret(cfg.refresh_token_enc)
    body = urllib.parse.urlencode(
        {
            "client_id": google_client_id(),
            "client_secret": google_client_secret(),
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }
    ).encode("utf-8")
    res = _request_json(
        "POST",
        _token_url(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=body,
    )
    access_token = res.get("access_token", "")
    if not access_token:
        raise StoreError(401, "Failed to refresh Google Drive token. Reconnect Google Drive in Storage.")
    expires_in = int(res.get("expires_in", 3600) or 3600)
    cfg.access_token_enc = storage.encrypt_secret(access_token)
    cfg.token_expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    db.commit()
    return access_token


def _ensure_access_token(cfg: UserGoogleDrive, db: Session) -> str:
    if cfg.access_token_enc and cfg.token_expires_at:
        if auth.utc_aware(cfg.token_expires_at) > datetime.now(timezone.utc) + timedelta(seconds=60):
            return storage.decrypt_secret(cfg.access_token_enc)
    return _refresh_access_token(cfg, db)


def _find_or_create_root_folder(access_token: str) -> str:
    headers = {"Authorization": f"Bearer {access_token}"}
    q = f"name='{ROOT_FOLDER_NAME}' and mimeType='{FOLDER_MIME}' and 'root' in parents and trashed=false"
    params = urllib.parse.urlencode({"q": q, "fields": "files(id,name)", "pageSize": 1})
    found = _request_json("GET", f"{_drive_api_base()}/files?{params}", headers=headers)
    files = found.get("files") or []
    if files:
        return files[0]["id"]
    payload = json.dumps({"name": ROOT_FOLDER_NAME, "mimeType": FOLDER_MIME, "parents": ["root"]}).encode("utf-8")
    created = _request_json(
        "POST",
        f"{_drive_api_base()}/files?fields=id,name",
        headers={**headers, "Content-Type": "application/json; charset=UTF-8"},
        data=payload,
    )
    return created["id"]


def _evict_drive_snapshot(root_folder_id: str, rel_or_prefix: str, is_prefix: bool = False) -> None:
    with _SNAP_LOCK:
        if not is_prefix:
            _LAST_DRIVE_SNAPSHOT.pop((root_folder_id, rel_or_prefix), None)
            return
        prefix = f"{rel_or_prefix}/"
        victims = [
            k
            for k in _LAST_DRIVE_SNAPSHOT
            if k[0] == root_folder_id and (k[1] == rel_or_prefix or k[1].startswith(prefix))
        ]
        for k in victims:
            _LAST_DRIVE_SNAPSHOT.pop(k, None)


def _ts(iso_val: str | None) -> float:
    if not iso_val:
        return 0.0
    try:
        return datetime.fromisoformat(iso_val.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


class DriveStore:
    def __init__(self, cfg: UserGoogleDrive, db: Session):
        self.cfg = cfg
        self.db = db
        self.root_folder_id = cfg.root_folder_id

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        token = _ensure_access_token(self.cfg, self.db)
        out = {"Authorization": f"Bearer {token}"}
        if extra:
            out.update(extra)
        return out

    def _api_json(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = self._headers({"Content-Type": "application/json; charset=UTF-8"} if body is not None else None)
        return _request_json(method, f"{_drive_api_base()}{path}", headers=headers, data=data)

    def _upload_multipart(self, meta: dict, html_text: str) -> dict:
        boundary = f"hn_{secrets.token_hex(12)}"
        body = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(meta)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: text/html; charset=UTF-8\r\n\r\n"
            f"{html_text}\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")
        headers = self._headers({"Content-Type": f"multipart/related; boundary={boundary}"})
        return _request_json(
            "POST",
            f"{_drive_upload_base()}/files?uploadType=multipart&fields=id,name",
            headers=headers,
            data=body,
        )

    def _upload_media(self, file_id: str, html_text: str) -> dict:
        headers = self._headers({"Content-Type": "text/html; charset=UTF-8"})
        return _request_json(
            "PATCH",
            f"{_drive_upload_base()}/files/{file_id}?uploadType=media&fields=id,name",
            headers=headers,
            data=html_text.encode("utf-8"),
        )

    def _download_text(self, file_id: str) -> str:
        raw = _request_raw("GET", f"{_drive_api_base()}/files/{file_id}?alt=media", headers=self._headers())
        return raw.decode("utf-8")

    def _list_all(self) -> dict[str, list[dict]]:
        by_parent: dict[str, list[dict]] = {}
        page_token: str | None = None
        while True:
            query: dict[str, str | int] = {
                "q": "trashed=false",
                "fields": "nextPageToken,files(id,name,mimeType,parents,createdTime,size)",
                "pageSize": 1000,
            }
            if page_token:
                query["pageToken"] = page_token
            params = urllib.parse.urlencode(query)
            res = _request_json("GET", f"{_drive_api_base()}/files?{params}", headers=self._headers())
            for item in res.get("files") or []:
                for pid in item.get("parents") or []:
                    by_parent.setdefault(pid, []).append(item)
            page_token = res.get("nextPageToken")
            if not page_token:
                return by_parent

    def _create_dir(self, parent_id: str, name: str) -> dict:
        return self._api_json("POST", "/files?fields=id,name", {"name": name, "mimeType": FOLDER_MIME, "parents": [parent_id]})

    def split_rel(self, rel: str | None) -> list[str]:
        parts = [p for p in (rel or "").split("/") if p]
        if any(p.startswith(".") or "\\" in p or "\x00" in p for p in parts):
            raise StoreError(400, "Invalid path")
        return parts

    def clean_name(self, name: str | None) -> str:
        clean = BAD_CHARS.sub("", str(name or "")).strip()
        if not clean or clean.startswith("."):
            raise StoreError(400, "Invalid name")
        return clean

    def page_rel(self, rel: str) -> str:
        parts = self.split_rel(rel)
        if len(parts) != 3 or not PAGE_EXT.search(parts[2]):
            raise StoreError(400, "Not a page")
        return "/".join(parts)

    def _find_child(self, by_parent: dict[str, list[dict]], parent_id: str, name: str, want_folder: bool | None = None) -> dict | None:
        for item in by_parent.get(parent_id, []):
            if item.get("name") != name:
                continue
            is_dir = item.get("mimeType") == FOLDER_MIME
            if want_folder is not None and is_dir != want_folder:
                continue
            return item
        return None

    def _resolve_chain(self, by_parent: dict[str, list[dict]], names: list[str], leaf_is_folder: bool) -> dict | None:
        pid = self.root_folder_id
        for idx, name in enumerate(names):
            want_folder = True if idx < len(names) - 1 else leaf_is_folder
            item = self._find_child(by_parent, pid, name, want_folder=want_folder)
            if not item:
                return None
            pid = item["id"]
        return item

    def _ensure_chain(self, by_parent: dict[str, list[dict]], names: list[str]) -> str:
        pid = self.root_folder_id
        for name in names:
            item = self._find_child(by_parent, pid, name, want_folder=True)
            if not item:
                item = self._create_dir(pid, name)
                by_parent.setdefault(pid, []).append({**item, "mimeType": FOLDER_MIME, "parents": [pid]})
            pid = item["id"]
        return pid

    def page_exists(self, rel: str) -> bool:
        parts = self.split_rel(self.page_rel(rel))
        by_parent = self._list_all()
        return self._resolve_chain(by_parent, parts, leaf_is_folder=False) is not None

    def _read_order(self, by_parent: dict[str, list[dict]]) -> tuple[dict | None, dict]:
        node = self._find_child(by_parent, self.root_folder_id, ".order.json", want_folder=False)
        if not node:
            return None, empty_order()
        try:
            return node, parse_order(self._download_text(node["id"]))
        except FileNotFoundError:
            return None, empty_order()

    def _write_order(self, node: dict | None, order: dict) -> None:
        raw = json.dumps(order, separators=(",", ":")).encode("utf-8")
        if node:
            headers = self._headers({"Content-Type": "application/json; charset=UTF-8"})
            _request_json(
                "PATCH",
                f"{_drive_upload_base()}/files/{node['id']}?uploadType=media&fields=id,name",
                headers=headers,
                data=raw,
            )
            return
        boundary = f"hn_{secrets.token_hex(12)}"
        meta = {"name": ".order.json", "mimeType": "application/json", "parents": [self.root_folder_id]}
        body = (
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(meta)}\r\n"
            f"--{boundary}\r\n"
            f"Content-Type: application/json; charset=UTF-8\r\n\r\n"
        ).encode("utf-8") + raw + f"\r\n--{boundary}--\r\n".encode("utf-8")
        headers = self._headers({"Content-Type": f"multipart/related; boundary={boundary}"})
        _request_json(
            "POST",
            f"{_drive_upload_base()}/files?uploadType=multipart&fields=id,name",
            headers=headers,
            data=body,
        )

    def tree(self) -> list[dict]:
        by_parent = self._list_all()
        _, order = self._read_order(by_parent)
        notebooks = apply_order(
            [
                item
                for item in by_parent.get(self.root_folder_id, [])
                if item.get("mimeType") == FOLDER_MIME and not item["name"].startswith(".")
            ],
            lambda item: item["name"],
            order["notebooks"],
            lambda item: (_ts(item.get("createdTime")), item["name"].lower()),
        )
        out: list[dict] = []
        for nb in notebooks:
            secs = apply_order(
                [
                    item
                    for item in by_parent.get(nb["id"], [])
                    if item.get("mimeType") == FOLDER_MIME and not item["name"].startswith(".")
                ],
                lambda item: item["name"],
                order["sections"].get(nb["name"], []),
                lambda item: (_ts(item.get("createdTime")), item["name"].lower()),
            )
            sections = []
            for sec in secs:
                raw_pages = [
                    item
                    for item in by_parent.get(sec["id"], [])
                    if item.get("mimeType") != FOLDER_MIME
                    and not item["name"].startswith(".")
                    and PAGE_EXT.search(item["name"])
                ]
                ordered_pages = apply_order(
                    raw_pages,
                    lambda item: item["name"],
                    order["pages"].get(f"{nb['name']}/{sec['name']}", []),
                    lambda item: (_ts(item.get("createdTime")), item["name"].lower()),
                )
                pages = [
                    {"name": PAGE_EXT.sub("", item["name"]), "path": f"{nb['name']}/{sec['name']}/{item['name']}"}
                    for item in ordered_pages
                ]
                sections.append({"name": sec["name"], "path": f"{nb['name']}/{sec['name']}", "pages": pages})
            out.append({"name": nb["name"], "path": nb["name"], "sections": sections})
        return out

    def create_folder(self, parent: str, name: str) -> dict:
        parts = self.split_rel(parent)
        if len(parts) > 1:
            raise StoreError(400, "Sections belong to a notebook")
        by_parent = self._list_all()
        parent_id = self.root_folder_id
        if len(parts) == 1:
            nb = self._find_child(by_parent, self.root_folder_id, parts[0], want_folder=True)
            if not nb:
                raise StoreError(404, "Notebook not found")
            parent_id = nb["id"]
        base = self.clean_name(name)
        existing = {item["name"] for item in by_parent.get(parent_id, []) if item.get("mimeType") == FOLDER_MIME}
        candidate = base
        i = 2
        while candidate in existing:
            candidate = f"{base} {i}"
            i += 1
        self._create_dir(parent_id, candidate)
        order_node, order = self._read_order(by_parent)
        order_append_folder(order, parts, candidate)
        self._write_order(order_node, order)
        rel = f"{parts[0]}/{candidate}" if parts else candidate
        return {"path": rel}

    def create_page(self, section: str, name: str, html: str) -> dict:
        parts = self.split_rel(section)
        if len(parts) != 2:
            raise StoreError(400, "Pages belong to a section")
        by_parent = self._list_all()
        sec_node = self._resolve_chain(by_parent, parts, leaf_is_folder=True)
        if not sec_node:
            raise StoreError(404, "Section not found")
        existing = {item["name"] for item in by_parent.get(sec_node["id"], []) if item.get("mimeType") != FOLDER_MIME}
        base = PAGE_EXT.sub("", self.clean_name(name))
        i = 1
        while True:
            filename = f"{base if i == 1 else f'{base} ({i})'}.html"
            if filename not in existing:
                break
            i += 1
        stem = PAGE_EXT.sub("", filename)
        content = sync_html_title(html, stem)
        self._upload_multipart({"name": filename, "mimeType": "text/html", "parents": [sec_node["id"]]}, content)
        sec_rel = f"{parts[0]}/{parts[1]}"
        order_node, order = self._read_order(by_parent)
        order_append_page(order, sec_rel, filename)
        self._write_order(order_node, order)
        return {"path": f"{sec_rel}/{filename}"}

    def read_page(self, rel: str) -> str:
        parts = self.split_rel(self.page_rel(rel))
        by_parent = self._list_all()
        item = self._resolve_chain(by_parent, parts, leaf_is_folder=False)
        if not item:
            raise FileNotFoundError()
        return self._download_text(item["id"])

    def _version_entries(self, by_parent: dict[str, list[dict]], clean: str) -> tuple[str | None, list[dict]]:
        parts = clean.split("/")
        folder = self._resolve_chain(by_parent, [".versions", *parts], leaf_is_folder=True)
        if not folder:
            return None, []
        items = [
            item
            for item in by_parent.get(folder["id"], [])
            if item.get("mimeType") != FOLDER_MIME and VERSION_ID.match(item["name"])
        ]
        items.sort(key=lambda item: int(item["name"].split(".")[0]))
        return folder["id"], items

    def write_page(self, rel: str, html: str, keep_version: bool) -> dict:
        clean = self.page_rel(rel)
        parts = clean.split("/")
        by_parent = self._list_all()
        item = self._resolve_chain(by_parent, parts, leaf_is_folder=False)
        if not item:
            raise FileNotFoundError()
        snap_key = (self.root_folder_id, clean)
        now = int(time.time() * 1000)
        if not keep_version:
            with _SNAP_LOCK:
                last = _LAST_DRIVE_SNAPSHOT.get(snap_key)
                if last is not None:
                    _LAST_DRIVE_SNAPSHOT.move_to_end(snap_key)
            if last is not None and now - last < VERSION_GAP_MS:
                self._upload_media(item["id"], html)
                return {"saved": True}
        prev = self._download_text(item["id"])
        if prev == html:
            return {"saved": False}
        ver_folder_id, ver_items = self._version_entries(by_parent, clean)
        if keep_version or not ver_items or now - int(ver_items[-1]["name"].split(".")[0]) >= VERSION_GAP_MS:
            snap_ts = now
            if ver_items and snap_ts <= int(ver_items[-1]["name"].split(".")[0]):
                snap_ts = int(ver_items[-1]["name"].split(".")[0]) + 1
            if not ver_folder_id:
                ver_folder_id = self._ensure_chain(by_parent, [".versions", *parts])
            snap = self._upload_multipart({"name": f"{snap_ts}.html", "mimeType": "text/html", "parents": [ver_folder_id]}, prev)
            ver_items.append({"id": snap["id"], "name": f"{snap_ts}.html"})
            for victim in ver_items[: max(0, len(ver_items) - MAX_VERSIONS)]:
                self._api_json("DELETE", f"/files/{victim['id']}")
        with _SNAP_LOCK:
            _LAST_DRIVE_SNAPSHOT[snap_key] = now
            _LAST_DRIVE_SNAPSHOT.move_to_end(snap_key)
            if len(_LAST_DRIVE_SNAPSHOT) > 2048:
                _LAST_DRIVE_SNAPSHOT.popitem(last=False)
        self._upload_media(item["id"], html)
        return {"saved": True}

    def list_versions(self, rel: str) -> list[dict]:
        clean = self.page_rel(rel)
        parts = clean.split("/")
        by_parent = self._list_all()
        if not self._resolve_chain(by_parent, parts, leaf_is_folder=False):
            raise FileNotFoundError()
        _, ver_items = self._version_entries(by_parent, clean)
        out = [
            {"id": item["name"], "savedAt": int(item["name"].split(".")[0]), "size": int(item.get("size", 0) or 0)}
            for item in ver_items
        ]
        return list(reversed(out))

    def read_version(self, rel: str, version_id: str) -> dict:
        clean = self.page_rel(rel)
        parts = clean.split("/")
        by_parent = self._list_all()
        if not self._resolve_chain(by_parent, parts, leaf_is_folder=False):
            raise FileNotFoundError()
        if not VERSION_ID.match(version_id):
            raise StoreError(400, "Invalid version")
        _, ver_items = self._version_entries(by_parent, clean)
        for item in ver_items:
            if item["name"] == version_id:
                return {"html": self._download_text(item["id"])}
        raise FileNotFoundError()

    def rename(self, rel: str, name: str) -> dict:
        parts = self.split_rel(rel)
        if not parts or len(parts) > 3:
            raise StoreError(400, "Invalid path")
        src_rel = "/".join(parts)
        is_page = len(parts) == 3 and PAGE_EXT.search(parts[2]) is not None
        if len(parts) == 3 and not is_page:
            raise StoreError(400, "Invalid path")
        by_parent = self._list_all()
        target = self._resolve_chain(by_parent, parts, leaf_is_folder=not is_page)
        if not target:
            raise FileNotFoundError()
        parent_id = (target.get("parents") or [self.root_folder_id])[0]
        if is_page:
            new_stem = PAGE_EXT.sub("", self.clean_name(name))
            new_leaf = f"{new_stem}.html"
            dst_rel = "/".join([*parts[:-1], new_leaf])
            if dst_rel == src_rel:
                return {"path": src_rel}
            if self._find_child(by_parent, parent_id, new_leaf, want_folder=False):
                raise StoreError(409, "Already exists")
            old_html = self._download_text(target["id"])
            updated_html = sync_html_title(old_html, new_stem)
            self._api_json("PATCH", f"/files/{target['id']}", {"name": new_leaf})
            self._upload_media(target["id"], updated_html)
            ver_node = self._resolve_chain(by_parent, [".versions", *parts], leaf_is_folder=True)
            if ver_node:
                self._api_json("PATCH", f"/files/{ver_node['id']}", {"name": new_leaf})
            _evict_drive_snapshot(self.root_folder_id, src_rel)
            order_node, order = self._read_order(by_parent)
            order_rename(order, parts, new_leaf)
            self._write_order(order_node, order)
            return {"path": dst_rel}
        new_leaf = self.clean_name(name)
        dst_rel = "/".join([*parts[:-1], new_leaf])
        if dst_rel == src_rel:
            return {"path": src_rel}
        if self._find_child(by_parent, parent_id, new_leaf, want_folder=True):
            raise StoreError(409, "Already exists")
        self._api_json("PATCH", f"/files/{target['id']}", {"name": new_leaf})
        ver_node = self._resolve_chain(by_parent, [".versions", *parts], leaf_is_folder=True)
        if ver_node:
            self._api_json("PATCH", f"/files/{ver_node['id']}", {"name": new_leaf})
        _evict_drive_snapshot(self.root_folder_id, src_rel, is_prefix=True)
        order_node, order = self._read_order(by_parent)
        order_rename(order, parts, new_leaf)
        self._write_order(order_node, order)
        return {"path": dst_rel}

    def remove(self, rel: str) -> dict:
        parts = self.split_rel(rel)
        if not parts or len(parts) > 3:
            raise StoreError(400, "Invalid path")
        src_rel = "/".join(parts)
        is_page = len(parts) == 3 and PAGE_EXT.search(parts[2]) is not None
        if len(parts) == 3 and not is_page:
            raise StoreError(400, "Invalid path")
        by_parent = self._list_all()
        target = self._resolve_chain(by_parent, parts, leaf_is_folder=not is_page)
        if not target:
            raise FileNotFoundError()
        self._api_json("DELETE", f"/files/{target['id']}")
        ver_node = self._resolve_chain(by_parent, [".versions", *parts], leaf_is_folder=True)
        if ver_node:
            self._api_json("DELETE", f"/files/{ver_node['id']}")
        _evict_drive_snapshot(self.root_folder_id, src_rel, is_prefix=not is_page)
        order_node, order = self._read_order(by_parent)
        order_remove(order, parts)
        self._write_order(order_node, order)
        return {"removed": True}

    def reorder(self, kind: str, parent: str, items: list[str]) -> dict:
        by_parent = self._list_all()
        order_node, order = self._read_order(by_parent)
        order_set(order, kind, parent, items, self.split_rel)
        self._write_order(order_node, order)
        return {"ok": True}


def drive_store_for_user(user_id: str, db: Session) -> DriveStore:
    cfg = db.get(UserGoogleDrive, user_id)
    if not cfg:
        raise StoreError(400, "Google Drive is not connected")
    return DriveStore(cfg, db)


@router.get("/auth-url")
def drive_auth_url(request: Request, user: User = Depends(auth.current_user)):
    if not drive_oauth_available():
        raise StoreError(
            400,
            "Google Drive OAuth is not configured on the server yet (set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET).",
        )
    params = urllib.parse.urlencode(
        {
            "client_id": google_client_id(),
            "redirect_uri": resolve_redirect_uri(request),
            "response_type": "code",
            "scope": DRIVE_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": make_oauth_state(user.id),
        }
    )
    return {"url": f"{_auth_url()}?{params}"}


def _google_login_user(access_token: str, db: Session) -> User:
    uinfo = _request_json("GET", _userinfo_url(), headers={"Authorization": f"Bearer {access_token}"})
    if uinfo.get("verified_email") is not True:
        raise StoreError(400, "Your Google account email is not verified")
    email = auth.clean_email(str(uinfo.get("email") or ""))
    user = db.scalar(select(User).where(User.email == email))
    if not user:
        user = User(id=uuid.uuid4().hex, email=email, password_hash=auth.hash_password(secrets.token_hex(32)))
        db.add(user)
        db.flush()
    return user


@router.get("/login")
def drive_login(request: Request):
    if not drive_oauth_available():
        return RedirectResponse(
            f"/?drive_error={urllib.parse.quote_plus('Google sign-in is not configured on the server')}", status_code=302
        )
    nonce = secrets.token_urlsafe(16)
    params = urllib.parse.urlencode(
        {
            "client_id": google_client_id(),
            "redirect_uri": resolve_redirect_uri(request),
            "response_type": "code",
            "scope": DRIVE_SCOPES,
            "access_type": "offline",
            "prompt": "consent",
            "state": make_oauth_state(f"login:{nonce}"),
        }
    )
    response = RedirectResponse(f"{_auth_url()}?{params}", status_code=302)
    response.set_cookie(
        OAUTH_LOGIN_COOKIE, nonce, max_age=600, path="/", secure=auth.COOKIE_SECURE, httponly=True, samesite="lax"
    )
    return response


@router.get("/callback")
def drive_callback(request: Request, code: str = "", state: str = "", error: str = "", db: Session = Depends(get_db)):
    def fail(msg: str) -> RedirectResponse:
        return RedirectResponse(f"/?drive_error={urllib.parse.quote_plus(msg)}", status_code=302)

    login_nonce = request.cookies.get(OAUTH_LOGIN_COOKIE, "")
    login_mode = bool(login_nonce) and verify_oauth_state(state, f"login:{login_nonce}")
    user = None if login_mode else auth.optional_user(request, db)
    if not login_mode and not user:
        return fail("Sign in first before connecting Google Drive")
    if error or not code:
        return fail(error or "Google Drive authorization was cancelled")
    if not drive_oauth_available():
        return fail("Google Drive OAuth is not configured on the server")
    if not login_mode and not verify_oauth_state(state, user.id):
        return fail("Google Drive authorization expired; please try again")
    try:
        tokens = _exchange_code(code, resolve_redirect_uri(request))
        access_token = tokens.get("access_token", "")
        if not access_token:
            return fail("Google did not return an access token")
        if login_mode:
            user = _google_login_user(access_token, db)
        existing = db.get(UserGoogleDrive, user.id)
        refresh_token = tokens.get("refresh_token", "")
        refresh_enc = storage.encrypt_secret(refresh_token) if refresh_token else (existing.refresh_token_enc if existing else "")
        if not refresh_enc:
            return fail("Google did not return a refresh token; remove app access and reconnect")
        expires_in = int(tokens.get("expires_in", 3600) or 3600)
        google_email = existing.google_email if existing else ""
        try:
            uinfo = _request_json("GET", _userinfo_url(), headers={"Authorization": f"Bearer {access_token}"})
            google_email = str(uinfo.get("email") or google_email or "").strip()
        except StoreError:
            pass
        root_id = _find_or_create_root_folder(access_token)
        access_enc = storage.encrypt_secret(access_token)
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        if existing:
            existing.google_email = google_email
            existing.refresh_token_enc = refresh_enc
            existing.access_token_enc = access_enc
            existing.token_expires_at = expires_at
            existing.root_folder_id = root_id
            existing.root_folder_name = ROOT_FOLDER_NAME
            existing.active = True
        else:
            db.add(
                UserGoogleDrive(
                    user_id=user.id,
                    google_email=google_email,
                    refresh_token_enc=refresh_enc,
                    access_token_enc=access_enc,
                    token_expires_at=expires_at,
                    root_folder_id=root_id,
                    root_folder_name=ROOT_FOLDER_NAME,
                    active=True,
                )
            )
        db.commit()
        response = RedirectResponse("/?drive=connected", status_code=302)
        if login_mode:
            auth.start_session(db, response, user)
            response.delete_cookie(OAUTH_LOGIN_COOKIE, path="/", secure=auth.COOKIE_SECURE, httponly=True, samesite="lax")
        return response
    except StoreError as err:
        return fail(err.message)


@router.post("/provider")
def switch_provider(body: ProviderIn, user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    provider = body.provider.strip().lower()
    if provider not in ("gdrive", "s3"):
        raise StoreError(400, "Invalid storage provider")
    drive = db.get(UserGoogleDrive, user.id)
    if provider == "gdrive":
        if not drive:
            raise StoreError(400, "Connect Google Drive first")
        drive.active = True
    elif drive:
        drive.active = False
    db.commit()
    return storage.public_storage_for_user(user.id, db)


@router.delete("")
def disconnect_drive(user: User = Depends(auth.current_user), db: Session = Depends(get_db)):
    drive = db.get(UserGoogleDrive, user.id)
    if drive:
        db.delete(drive)
        db.commit()
    return storage.public_storage_for_user(user.id, db)
