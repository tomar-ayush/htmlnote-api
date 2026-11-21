from collections import OrderedDict
import hashlib
from html import escape as html_escape
import ipaddress
import json
import os
import re
import socket
import threading
import time
from urllib.parse import urlsplit

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

MAX_VERSIONS = 10
VERSION_GAP_MS = 5 * 60 * 1000
PAGE_EXT = re.compile(r"\.html?$", re.IGNORECASE)
VERSION_ID = re.compile(r"^\d+\.html$")
BAD_CHARS = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
TITLE_TAG = re.compile(r"<title\b[^>]*>.*?</title>", re.IGNORECASE | re.DOTALL)
HEAD_OPEN = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
HTML_OPEN = re.compile(r"<html\b[^>]*>", re.IGNORECASE)
DOCTYPE_LINE = re.compile(r"^\s*<!doctype[^>]*>\s*", re.IGNORECASE)


def sync_html_title(raw_html: str, title: str) -> str:
    clean_title = (title or "").strip() or "Untitled page"
    tag = f"<title>{html_escape(clean_title)}</title>"
    if not raw_html.strip():
        return (
            '<!DOCTYPE html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n'
            f"{tag}\n</head>\n<body><p><br></p></body>\n</html>\n"
        )
    if TITLE_TAG.search(raw_html):
        return TITLE_TAG.sub(lambda _: tag, raw_html, count=1)
    m = HEAD_OPEN.search(raw_html)
    if m:
        return raw_html[: m.end()] + "\n" + tag + raw_html[m.end() :]
    m = HTML_OPEN.search(raw_html)
    if m:
        return raw_html[: m.end()] + f"\n<head>{tag}</head>" + raw_html[m.end() :]
    m = DOCTYPE_LINE.match(raw_html)
    if m:
        return raw_html[: m.end()] + f"<head>{tag}</head>\n" + raw_html[m.end() :]
    return f"<head>{tag}</head>\n{raw_html}"


def empty_order() -> dict:
    return {"notebooks": [], "sections": {}, "pages": {}}


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def parse_order(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return empty_order()
    if not isinstance(data, dict):
        return empty_order()
    nbs = _dedupe([str(x) for x in data.get("notebooks", []) if isinstance(x, str)])
    secs_raw = data.get("sections", {})
    secs = (
        {str(k): _dedupe([str(x) for x in v if isinstance(x, str)]) for k, v in secs_raw.items() if isinstance(v, list)}
        if isinstance(secs_raw, dict)
        else {}
    )
    pages_raw = data.get("pages", {})
    pages = (
        {str(k): _dedupe([str(x) for x in v if isinstance(x, str)]) for k, v in pages_raw.items() if isinstance(v, list)}
        if isinstance(pages_raw, dict)
        else {}
    )
    return {"notebooks": nbs, "sections": secs, "pages": pages}


def apply_order(items: list, key_fn, preferred: list[str], fallback_key) -> list:
    if not preferred:
        return sorted(items, key=fallback_key)
    rank = {name: idx for idx, name in enumerate(preferred)}
    known = sorted((item for item in items if key_fn(item) in rank), key=lambda item: rank[key_fn(item)])
    rest = sorted((item for item in items if key_fn(item) not in rank), key=fallback_key)
    return [*known, *rest]


def order_append_folder(order: dict, parent_parts: list[str], leaf: str) -> dict:
    if not parent_parts:
        if leaf not in order["notebooks"]:
            order["notebooks"].append(leaf)
    elif len(parent_parts) == 1:
        sec_list = order["sections"].setdefault(parent_parts[0], [])
        if leaf not in sec_list:
            sec_list.append(leaf)
    return order


def order_append_page(order: dict, sec_rel: str, filename: str) -> dict:
    page_list = order["pages"].setdefault(sec_rel, [])
    if filename not in page_list:
        page_list.append(filename)
    return order


def order_rename(order: dict, src_parts: list[str], new_leaf: str) -> dict:
    if len(src_parts) == 1:
        old_nb = src_parts[0]
        order["notebooks"] = _dedupe([new_leaf if x == old_nb else x for x in order["notebooks"]])
        if old_nb in order["sections"]:
            order["sections"][new_leaf] = order["sections"].pop(old_nb)
        old_prefix = f"{old_nb}/"
        for k in [key for key in order["pages"] if key.startswith(old_prefix)]:
            order["pages"][f"{new_leaf}/{k[len(old_prefix):]}"] = order["pages"].pop(k)
    elif len(src_parts) == 2:
        nb, old_sec = src_parts
        if nb in order["sections"]:
            order["sections"][nb] = _dedupe([new_leaf if x == old_sec else x for x in order["sections"][nb]])
        old_sec_key = f"{nb}/{old_sec}"
        if old_sec_key in order["pages"]:
            order["pages"][f"{nb}/{new_leaf}"] = order["pages"].pop(old_sec_key)
    elif len(src_parts) == 3:
        nb, sec, old_file = src_parts
        sec_key = f"{nb}/{sec}"
        if sec_key in order["pages"]:
            order["pages"][sec_key] = _dedupe([new_leaf if x == old_file else x for x in order["pages"][sec_key]])
    return order


def order_remove(order: dict, src_parts: list[str]) -> dict:
    if len(src_parts) == 1:
        nb = src_parts[0]
        order["notebooks"] = [x for x in order["notebooks"] if x != nb]
        order["sections"].pop(nb, None)
        prefix = f"{nb}/"
        for k in [key for key in order["pages"] if key.startswith(prefix)]:
            order["pages"].pop(k, None)
    elif len(src_parts) == 2:
        nb, sec = src_parts
        if nb in order["sections"]:
            order["sections"][nb] = [x for x in order["sections"][nb] if x != sec]
        order["pages"].pop(f"{nb}/{sec}", None)
    elif len(src_parts) == 3:
        nb, sec, filename = src_parts
        sec_key = f"{nb}/{sec}"
        if sec_key in order["pages"]:
            order["pages"][sec_key] = [x for x in order["pages"][sec_key] if x != filename]
    return order


def order_set(order: dict, kind: str, parent: str, items: list[str], split_rel_fn) -> dict:
    if kind == "notebooks":
        if split_rel_fn(parent):
            raise StoreError(400, "Invalid parent")
        leaves = [parts[-1] for parts in (split_rel_fn(x) for x in items) if parts]
        order["notebooks"] = _dedupe(leaves)
        return order
    if kind == "sections":
        pp = split_rel_fn(parent)
        if len(pp) != 1:
            raise StoreError(400, "Invalid parent")
        leaves = [parts[-1] for parts in (split_rel_fn(x) for x in items) if parts]
        order["sections"][pp[0]] = _dedupe(leaves)
        return order
    if kind == "pages":
        pp = split_rel_fn(parent)
        if len(pp) != 2:
            raise StoreError(400, "Invalid parent")
        leaves = []
        for x in items:
            parts = split_rel_fn(x)
            if not parts:
                continue
            leaf = parts[-1]
            leaves.append(leaf if PAGE_EXT.search(leaf) else f"{leaf}.html")
        order["pages"][f"{pp[0]}/{pp[1]}"] = _dedupe(leaves)
        return order
    raise StoreError(400, "Invalid reorder target")

_CACHE_LOCK = threading.Lock()
_ENDPOINT_CACHE: OrderedDict[tuple[str, bool], str] = OrderedDict()
_CLIENT_CACHE: OrderedDict[tuple[str, str, str, str, bool], object] = OrderedDict()
_LAST_SNAPSHOT: OrderedDict[tuple[str, str, str], int] = OrderedDict()
_USAGE_CACHE: OrderedDict[tuple[str, str, str], tuple[int, float]] = OrderedDict()
USAGE_TTL_SEC = 15.0


class StoreError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def validate_endpoint(endpoint_url: str) -> str:
    cleaned = (endpoint_url or "").strip().rstrip("/")
    if not cleaned:
        return ""
    allow_insecure = os.environ.get("ALLOW_INSECURE_S3", "false").lower() == "true"
    cache_key = (cleaned, allow_insecure)
    with _CACHE_LOCK:
        cached = _ENDPOINT_CACHE.get(cache_key)
        if cached is not None:
            _ENDPOINT_CACHE.move_to_end(cache_key)
            return cached
    u = urlsplit(cleaned)
    allowed = ("https", "http") if allow_insecure else ("https",)
    if u.scheme.lower() not in allowed:
        raise StoreError(400, "Endpoint URL must start with https://")
    if not u.hostname or u.username or u.password or u.query or u.fragment:
        raise StoreError(400, "Invalid S3 endpoint URL")
    if u.path:
        raise StoreError(400, "Endpoint URL must not include a path or bucket name")
    if not allow_insecure:
        try:
            infos = socket.getaddrinfo(u.hostname, u.port or 443, type=socket.SOCK_STREAM)
        except socket.gaierror:
            raise StoreError(400, "Cannot resolve S3 endpoint host")
        for *_, sockaddr in infos:
            ip = ipaddress.ip_address(sockaddr[0].split("%")[0])
            target = getattr(ip, "ipv4_mapped", None) or ip
            if not target.is_global:
                raise StoreError(400, "Private or internal endpoint addresses are not allowed")
    normalized = f"{u.scheme.lower()}://{u.netloc}"
    with _CACHE_LOCK:
        _ENDPOINT_CACHE[cache_key] = normalized
        if len(_ENDPOINT_CACHE) > 256:
            _ENDPOINT_CACHE.popitem(last=False)
    return normalized


def _get_client(endpoint_url: str, region: str, access_key_id: str, secret_access_key: str, force_path_style: bool):
    secret_digest = hashlib.sha256(secret_access_key.encode("utf-8")).hexdigest()
    key = (endpoint_url, region, access_key_id, secret_digest, bool(force_path_style))
    with _CACHE_LOCK:
        cached = _CLIENT_CACHE.get(key)
        if cached is not None:
            _CLIENT_CACHE.move_to_end(key)
            return cached
    cfg = Config(
        region_name=region,
        signature_version="s3v4",
        request_checksum_calculation="when_required",
        response_checksum_validation="when_required",
        connect_timeout=5,
        read_timeout=15,
        max_pool_connections=16,
        retries={"max_attempts": 2, "mode": "standard"},
        s3={"addressing_style": "path" if force_path_style else "auto"},
    )
    client = boto3.client(
        "s3",
        endpoint_url=endpoint_url or None,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        region_name=region,
        config=cfg,
    )
    with _CACHE_LOCK:
        _CLIENT_CACHE[key] = client
        if len(_CLIENT_CACHE) > 64:
            _CLIENT_CACHE.popitem(last=False)
    return client


def _evict_snapshot(endpoint_url: str, bucket: str, key_or_prefix: str, is_prefix: bool = False) -> None:
    with _CACHE_LOCK:
        if not is_prefix:
            _LAST_SNAPSHOT.pop((endpoint_url, bucket, key_or_prefix), None)
            return
        victims = [
            k
            for k in _LAST_SNAPSHOT
            if k[0] == endpoint_url and k[1] == bucket and k[2].startswith(key_or_prefix)
        ]
        for k in victims:
            _LAST_SNAPSHOT.pop(k, None)


def clean_prefix(prefix: str | None) -> str:
    parts = [p for p in (prefix or "").split("/") if p]
    if any(p.startswith(".") or BAD_CHARS.search(p) for p in parts):
        raise StoreError(400, "Invalid prefix")
    return "/".join(parts)


class Store:
    def __init__(
        self,
        endpoint_url: str,
        region: str,
        bucket: str,
        access_key_id: str,
        secret_access_key: str,
        prefix: str = "",
        force_path_style: bool = False,
        quota_bytes: int | None = None,
    ):
        self.endpoint_url = validate_endpoint(endpoint_url)
        self.region = (region or "").strip() or "auto"
        self.bucket = (bucket or "").strip()
        cleaned = clean_prefix(prefix)
        self.base_prefix = f"{cleaned}/" if cleaned else ""
        self.quota_bytes = quota_bytes
        self.client = _get_client(self.endpoint_url, self.region, access_key_id, secret_access_key, force_path_style)

    def _usage_key(self) -> tuple[str, str, str]:
        return (self.endpoint_url, self.bucket, self.base_prefix)

    def used_bytes(self, refresh: bool = False) -> int:
        ukey = self._usage_key()
        if not refresh:
            with _CACHE_LOCK:
                entry = _USAGE_CACHE.get(ukey)
                if entry is not None and time.monotonic() - entry[1] < USAGE_TTL_SEC:
                    _USAGE_CACHE.move_to_end(ukey)
                    return entry[0]
        total = sum(int(obj.get("Size", 0)) for obj in self._list(""))
        with _CACHE_LOCK:
            _USAGE_CACHE[ukey] = (total, time.monotonic())
            _USAGE_CACHE.move_to_end(ukey)
            if len(_USAGE_CACHE) > 512:
                _USAGE_CACHE.popitem(last=False)
        return total

    def _adjust_usage(self, delta: int) -> None:
        ukey = self._usage_key()
        with _CACHE_LOCK:
            entry = _USAGE_CACHE.get(ukey)
            if entry is not None:
                _USAGE_CACHE[ukey] = (max(0, entry[0] + delta), entry[1])

    def _invalidate_usage(self) -> None:
        ukey = self._usage_key()
        with _CACHE_LOCK:
            _USAGE_CACHE.pop(ukey, None)

    def _check_quota(self, added_bytes: int) -> None:
        if self.quota_bytes is None:
            return
        needed = max(0, added_bytes)
        if self.used_bytes() + needed <= self.quota_bytes:
            return
        if self.used_bytes(refresh=True) + needed <= self.quota_bytes:
            return
        limit_mb = max(1, round(self.quota_bytes / (1024 * 1024)))
        raise StoreError(
            413,
            f"Free {limit_mb} MB storage limit reached. Connect your own S3 or R2 bucket in Storage for unlimited space.",
        )

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ClientError as err:
            error = err.response.get("Error", {})
            code = str(error.get("Code", ""))
            if code in ("NoSuchKey", "404", "NotFound"):
                raise FileNotFoundError()
            if code == "NoSuchBucket":
                raise StoreError(400, f'Bucket "{self.bucket}" was not found')
            if code in ("InvalidAccessKeyId", "SignatureDoesNotMatch"):
                raise StoreError(400, "Invalid S3 credentials or region mismatch")
            if code in ("AccessDenied", "AllAccessDisabled", "AccountProblem"):
                raise StoreError(403, "Access denied by S3 bucket — check key permissions")
            raise StoreError(400, str(error.get("Message") or "S3 request failed"))
        except BotoCoreError:
            raise StoreError(502, "Cannot connect to S3 endpoint")

    def full_key(self, rel: str) -> str:
        return f"{self.base_prefix}{rel}"

    def strip_prefix(self, key: str) -> str:
        if self.base_prefix and key.startswith(self.base_prefix):
            return key[len(self.base_prefix) :]
        return key

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

    def _list(self, rel_prefix: str = "") -> list[dict]:
        items: list[dict] = []
        token: str | None = None
        prefix = self.full_key(rel_prefix)
        while True:
            kwargs: dict = {"Bucket": self.bucket, "Prefix": prefix}
            if token:
                kwargs["ContinuationToken"] = token
            res = self._call(self.client.list_objects_v2, **kwargs)
            items.extend(res.get("Contents", []))
            if not res.get("IsTruncated"):
                return items
            token = res.get("NextContinuationToken")
            if not token:
                return items

    def _list_one(self, rel_prefix: str) -> bool:
        res = self._call(self.client.list_objects_v2, Bucket=self.bucket, Prefix=self.full_key(rel_prefix), MaxKeys=1)
        return bool(res.get("Contents"))

    def _list_prefixes(self, rel_prefix: str) -> set[str]:
        names: set[str] = set()
        token: str | None = None
        prefix = self.full_key(rel_prefix)
        while True:
            kwargs: dict = {"Bucket": self.bucket, "Prefix": prefix, "Delimiter": "/"}
            if token:
                kwargs["ContinuationToken"] = token
            res = self._call(self.client.list_objects_v2, **kwargs)
            for item in res.get("CommonPrefixes", []):
                raw = item.get("Prefix", "")[len(prefix) :].rstrip("/")
                if raw and not raw.startswith("."):
                    names.add(raw)
            if not res.get("IsTruncated"):
                return names
            token = res.get("NextContinuationToken")
            if not token:
                return names

    def _head_exists(self, key: str) -> bool:
        try:
            self._call(self.client.head_object, Bucket=self.bucket, Key=key)
            return True
        except FileNotFoundError:
            return False

    def _get_text(self, key: str) -> str:
        res = self._call(self.client.get_object, Bucket=self.bucket, Key=key)
        body = res["Body"]
        try:
            return body.read().decode("utf-8")
        finally:
            body.close()

    def _put_text(self, key: str, text: str) -> None:
        self._call(
            self.client.put_object,
            Bucket=self.bucket,
            Key=key,
            Body=text.encode("utf-8"),
            ContentType="text/html; charset=utf-8",
        )

    def _put_keep(self, rel_dir: str) -> None:
        self._call(self.client.put_object, Bucket=self.bucket, Key=self.full_key(f"{rel_dir}/.keep"), Body=b"")

    def _copy_key(self, old_key: str, new_key: str) -> None:
        self._call(
            self.client.copy_object,
            CopySource={"Bucket": self.bucket, "Key": old_key},
            Bucket=self.bucket,
            Key=new_key,
        )

    def _delete_keys(self, keys: list[str]) -> None:
        for i in range(0, len(keys), 1000):
            chunk = keys[i : i + 1000]
            try:
                res = self._call(
                    self.client.delete_objects,
                    Bucket=self.bucket,
                    Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True},
                )
                for err in res.get("Errors") or []:
                    if err.get("Key"):
                        self._call(self.client.delete_object, Bucket=self.bucket, Key=err["Key"])
            except StoreError:
                for key in chunk:
                    self._call(self.client.delete_object, Bucket=self.bucket, Key=key)

    def verify_access(self) -> None:
        self._call(self.client.list_objects_v2, Bucket=self.bucket, Prefix=self.base_prefix, MaxKeys=1)
        probe = self.full_key(".versions/.probe")
        self._call(self.client.put_object, Bucket=self.bucket, Key=probe, Body=b"ok", ContentType="text/plain")
        self._call(self.client.delete_object, Bucket=self.bucket, Key=probe)

    def _read_order(self) -> dict:
        try:
            return parse_order(self._get_text(self.full_key(".order.json")))
        except FileNotFoundError:
            return empty_order()

    def _write_order(self, order: dict) -> None:
        self._call(
            self.client.put_object,
            Bucket=self.bucket,
            Key=self.full_key(".order.json"),
            Body=json.dumps(order, separators=(",", ":")).encode("utf-8"),
            ContentType="application/json; charset=utf-8",
        )

    def tree(self) -> list[dict]:
        notebooks_map: dict[str, set[str]] = {}
        pages_map: dict[tuple[str, str], list[dict]] = {}
        nb_keep_ts: dict[str, float] = {}
        nb_min_ts: dict[str, float] = {}
        sec_keep_ts: dict[tuple[str, str], float] = {}
        sec_min_ts: dict[tuple[str, str], float] = {}
        page_min_ts: dict[tuple[str, str, str], float] = {}
        has_order = False

        def ensure_nb(nb: str, ts: float) -> None:
            notebooks_map.setdefault(nb, set())
            if nb not in nb_min_ts or ts < nb_min_ts[nb]:
                nb_min_ts[nb] = ts

        def ensure_sec(nb: str, sec: str, ts: float) -> None:
            ensure_nb(nb, ts)
            notebooks_map[nb].add(sec)
            pages_map.setdefault((nb, sec), [])
            key = (nb, sec)
            if key not in sec_min_ts or ts < sec_min_ts[key]:
                sec_min_ts[key] = ts

        for obj in self._list(""):
            rel = self.strip_prefix(obj["Key"])
            if not rel:
                continue
            lm = obj.get("LastModified")
            ts = lm.timestamp() if lm else 0.0
            if rel == ".order.json":
                has_order = True
                continue
            if rel.endswith("/"):
                parts = [p for p in rel.split("/") if p]
                if not parts or any(p.startswith(".") for p in parts):
                    continue
                if len(parts) == 1:
                    ensure_nb(parts[0], ts)
                else:
                    ensure_sec(parts[0], parts[1], ts)
                continue
            parts = rel.split("/")
            if parts[0] == ".versions" and len(parts) == 5 and VERSION_ID.match(parts[4]):
                vts = int(parts[4].split(".")[0]) / 1000.0
                pkey = (parts[1], parts[2], parts[3])
                if pkey not in page_min_ts or vts < page_min_ts[pkey]:
                    page_min_ts[pkey] = vts
                continue
            if parts[0].startswith("."):
                continue
            if len(parts) == 2 and parts[1] == ".keep":
                ensure_nb(parts[0], ts)
                nb_keep_ts[parts[0]] = ts
            elif len(parts) == 3 and parts[2] == ".keep" and not parts[1].startswith("."):
                ensure_sec(parts[0], parts[1], ts)
                sec_keep_ts[(parts[0], parts[1])] = ts
            elif len(parts) == 3 and not any(p.startswith(".") for p in parts) and PAGE_EXT.search(parts[2]):
                ensure_sec(parts[0], parts[1], ts)
                pkey = (parts[0], parts[1], parts[2])
                if pkey not in page_min_ts or ts < page_min_ts[pkey]:
                    page_min_ts[pkey] = ts
                pages_map[(parts[0], parts[1])].append(
                    {"name": PAGE_EXT.sub("", parts[2]), "path": rel, "leaf": parts[2]}
                )

        order = self._read_order() if has_order else empty_order()
        ordered_nbs = apply_order(
            list(notebooks_map.keys()),
            lambda n: n,
            order["notebooks"],
            lambda n: (nb_keep_ts.get(n, nb_min_ts.get(n, 0.0)), n.lower()),
        )
        result = []
        for nb in ordered_nbs:
            ordered_secs = apply_order(
                list(notebooks_map[nb]),
                lambda s: s,
                order["sections"].get(nb, []),
                lambda s: (sec_keep_ts.get((nb, s), sec_min_ts.get((nb, s), 0.0)), s.lower()),
            )
            sections = []
            for sec in ordered_secs:
                ordered_pages = apply_order(
                    pages_map[(nb, sec)],
                    lambda p: p["leaf"],
                    order["pages"].get(f"{nb}/{sec}", []),
                    lambda p: (page_min_ts.get((nb, sec, p["leaf"]), 0.0), p["name"].lower()),
                )
                pages = [{"name": p["name"], "path": p["path"]} for p in ordered_pages]
                sections.append({"name": sec, "path": f"{nb}/{sec}", "pages": pages})
            result.append({"name": nb, "path": nb, "sections": sections})
        return result

    def create_folder(self, parent: str, name: str) -> dict:
        parts = self.split_rel(parent)
        if len(parts) > 1:
            raise StoreError(400, "Sections belong to a notebook")
        if len(parts) == 1 and not self._list_one(f"{parts[0]}/"):
            raise StoreError(404, "Notebook not found")
        base = self.clean_name(name)
        parent_rel = f"{parts[0]}/" if parts else ""
        existing = self._list_prefixes(parent_rel)
        candidate = base
        i = 2
        while candidate in existing:
            candidate = f"{base} {i}"
            i += 1
        rel = f"{parent_rel}{candidate}"
        self._put_keep(rel)
        order = self._read_order()
        order_append_folder(order, parts, candidate)
        self._write_order(order)
        return {"path": rel}

    def create_page(self, section: str, name: str, html: str) -> dict:
        parts = self.split_rel(section)
        if len(parts) != 2:
            raise StoreError(400, "Pages belong to a section")
        sec_rel = "/".join(parts)
        objs = self._list(f"{sec_rel}/")
        if not objs:
            raise StoreError(404, "Section not found")
        prefix = self.full_key(f"{sec_rel}/")
        existing = {o["Key"][len(prefix) :] for o in objs}
        base = PAGE_EXT.sub("", self.clean_name(name))
        i = 1
        while True:
            filename = f"{base if i == 1 else f'{base} ({i})'}.html"
            if filename not in existing:
                break
            i += 1
        rel = f"{sec_rel}/{filename}"
        stem = PAGE_EXT.sub("", filename)
        content = sync_html_title(html, stem)
        added = len(content.encode("utf-8"))
        self._check_quota(added)
        self._put_text(self.full_key(rel), content)
        self._adjust_usage(added)
        order = self._read_order()
        order_append_page(order, sec_rel, filename)
        self._write_order(order)
        return {"path": rel}

    def page_exists(self, rel: str) -> bool:
        return self._head_exists(self.full_key(self.page_rel(rel)))

    def read_page(self, rel: str) -> str:
        return self._get_text(self.full_key(self.page_rel(rel)))

    def write_page(self, rel: str, html: str, keep_version: bool) -> dict:
        clean = self.page_rel(rel)
        key = self.full_key(clean)
        snap_key = (self.endpoint_url, self.bucket, key)
        now = self.now_ms()
        new_size = len(html.encode("utf-8"))
        if not keep_version:
            with _CACHE_LOCK:
                last = _LAST_SNAPSHOT.get(snap_key)
                if last is not None:
                    _LAST_SNAPSHOT.move_to_end(snap_key)
            if last is not None and now - last < VERSION_GAP_MS:
                if self.quota_bytes is None or self.used_bytes() + new_size <= self.quota_bytes:
                    self._put_text(key, html)
                    self._invalidate_usage()
                    return {"saved": True}
        prev = self._get_text(key)
        if prev == html:
            return {"saved": False}
        prev_size = len(prev.encode("utf-8"))
        items = self.version_items(clean) if self.quota_bytes is not None else []
        will_snap = keep_version or not items or now - int(items[-1][0].split(".")[0]) >= VERSION_GAP_MS
        self._check_quota(new_size if will_snap else max(0, new_size - prev_size))
        self.snapshot(clean, prev, keep_version)
        with _CACHE_LOCK:
            _LAST_SNAPSHOT[snap_key] = now
            _LAST_SNAPSHOT.move_to_end(snap_key)
            if len(_LAST_SNAPSHOT) > 2048:
                _LAST_SNAPSHOT.popitem(last=False)
        self._put_text(key, html)
        self._invalidate_usage()
        return {"saved": True}

    def now_ms(self) -> int:
        return int(time.time() * 1000)

    def version_items(self, rel: str) -> list[tuple[str, int]]:
        prefix = self.full_key(f".versions/{rel}/")
        items: list[tuple[str, int]] = []
        for obj in self._list(f".versions/{rel}/"):
            name = obj["Key"][len(prefix) :]
            if VERSION_ID.match(name):
                items.append((name, int(obj.get("Size", 0))))
        items.sort(key=lambda item: int(item[0].split(".")[0]))
        return items

    def snapshot(self, rel: str, html: str, force: bool) -> None:
        items = self.version_items(rel)
        now = self.now_ms()
        if not force and items and now - int(items[-1][0].split(".")[0]) < VERSION_GAP_MS:
            return
        if items and now <= int(items[-1][0].split(".")[0]):
            now = int(items[-1][0].split(".")[0]) + 1
        self._put_text(self.full_key(f".versions/{rel}/{now}.html"), html)
        items = self.version_items(rel)
        old = [self.full_key(f".versions/{rel}/{name}") for name, _ in items[: max(0, len(items) - MAX_VERSIONS)]]
        self._delete_keys(old)

    def list_versions(self, rel: str) -> list[dict]:
        clean = self.page_rel(rel)
        if not self._head_exists(self.full_key(clean)):
            raise FileNotFoundError()
        items = [{"id": name, "savedAt": int(name.split(".")[0]), "size": size} for name, size in self.version_items(clean)]
        return list(reversed(items))

    def read_version(self, rel: str, version_id: str) -> dict:
        clean = self.page_rel(rel)
        if not self._head_exists(self.full_key(clean)):
            raise FileNotFoundError()
        if not VERSION_ID.match(version_id):
            raise StoreError(400, "Invalid version")
        return {"html": self._get_text(self.full_key(f".versions/{clean}/{version_id}"))}

    def rename(self, rel: str, name: str) -> dict:
        parts = self.split_rel(rel)
        if not parts or len(parts) > 3:
            raise StoreError(400, "Invalid path")
        src_rel = "/".join(parts)
        if len(parts) == 3 and PAGE_EXT.search(parts[2]):
            src_key = self.full_key(src_rel)
            if not self._head_exists(src_key):
                raise FileNotFoundError()
            new_stem = PAGE_EXT.sub("", self.clean_name(name))
            dst_rel = "/".join([*parts[:-1], f"{new_stem}.html"])
            if dst_rel == src_rel:
                return {"path": src_rel}
            dst_key = self.full_key(dst_rel)
            if self._head_exists(dst_key):
                raise StoreError(409, "Already exists")
            old_html = self._get_text(src_key)
            updated_html = sync_html_title(old_html, new_stem)
            old_len = len(old_html.encode("utf-8"))
            new_len = len(updated_html.encode("utf-8"))
            if new_len > old_len:
                self._check_quota(new_len - old_len)
            self._put_text(dst_key, updated_html)
            old_ver_prefix = self.full_key(f".versions/{src_rel}/")
            new_ver_prefix = self.full_key(f".versions/{dst_rel}/")
            ver_objs = self._list(f".versions/{src_rel}/")
            for obj in ver_objs:
                self._copy_key(obj["Key"], new_ver_prefix + obj["Key"][len(old_ver_prefix) :])
            self._delete_keys([src_key, *(o["Key"] for o in ver_objs)])
            self._adjust_usage(new_len - old_len)
            _evict_snapshot(self.endpoint_url, self.bucket, src_key)
            order = self._read_order()
            order_rename(order, parts, f"{new_stem}.html")
            self._write_order(order)
            return {"path": dst_rel}
        if len(parts) not in (1, 2):
            raise StoreError(400, "Invalid path")
        old_objs = self._list(f"{src_rel}/")
        if not old_objs:
            raise FileNotFoundError()
        new_leaf = self.clean_name(name)
        dst_rel = "/".join([*parts[:-1], new_leaf])
        if dst_rel == src_rel:
            return {"path": src_rel}
        if self._list_one(f"{dst_rel}/"):
            raise StoreError(409, "Already exists")
        old_prefix = self.full_key(f"{src_rel}/")
        new_prefix = self.full_key(f"{dst_rel}/")
        for obj in old_objs:
            self._copy_key(obj["Key"], new_prefix + obj["Key"][len(old_prefix) :])
        old_ver_prefix = self.full_key(f".versions/{src_rel}/")
        new_ver_prefix = self.full_key(f".versions/{dst_rel}/")
        ver_objs = self._list(f".versions/{src_rel}/")
        for obj in ver_objs:
            self._copy_key(obj["Key"], new_ver_prefix + obj["Key"][len(old_ver_prefix) :])
        self._delete_keys([*(o["Key"] for o in old_objs), *(o["Key"] for o in ver_objs)])
        _evict_snapshot(self.endpoint_url, self.bucket, old_prefix, is_prefix=True)
        order = self._read_order()
        order_rename(order, parts, new_leaf)
        self._write_order(order)
        return {"path": dst_rel}

    def remove(self, rel: str) -> dict:
        parts = self.split_rel(rel)
        if not parts or len(parts) > 3:
            raise StoreError(400, "Invalid path")
        src_rel = "/".join(parts)
        if len(parts) == 3 and PAGE_EXT.search(parts[2]):
            src_key = self.full_key(src_rel)
            if not self._head_exists(src_key):
                raise FileNotFoundError()
            ver_objs = self._list(f".versions/{src_rel}/")
            self._delete_keys([src_key, *(o["Key"] for o in ver_objs)])
            _evict_snapshot(self.endpoint_url, self.bucket, src_key)
            self._invalidate_usage()
            sec_rel = f"{parts[0]}/{parts[1]}"
            if not self._list_one(f"{sec_rel}/"):
                self._put_keep(sec_rel)
            order = self._read_order()
            order_remove(order, parts)
            self._write_order(order)
            return {"removed": True}
        if len(parts) not in (1, 2):
            raise StoreError(400, "Invalid path")
        old_objs = self._list(f"{src_rel}/")
        if not old_objs:
            raise FileNotFoundError()
        ver_objs = self._list(f".versions/{src_rel}/")
        self._delete_keys([*(o["Key"] for o in old_objs), *(o["Key"] for o in ver_objs)])
        _evict_snapshot(self.endpoint_url, self.bucket, self.full_key(f"{src_rel}/"), is_prefix=True)
        self._invalidate_usage()
        if len(parts) == 2 and not self._list_one(f"{parts[0]}/"):
            self._put_keep(parts[0])
        order = self._read_order()
        order_remove(order, parts)
        self._write_order(order)
        return {"removed": True}

    def reorder(self, kind: str, parent: str, items: list[str]) -> dict:
        order = self._read_order()
        order_set(order, kind, parent, items, self.split_rel)
        self._write_order(order)
        return {"ok": True}
