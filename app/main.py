from fnmatch import fnmatchcase
import os
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from . import auth, share, storage
from .db import get_db
from .models import User
from .store import Store, StoreError

EDIT_CSP = "script-src 'none'; object-src 'none'; base-uri 'none'"
VIEW_CSP = "sandbox allow-scripts allow-popups allow-modals"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
STATIC_DIR = Path(os.environ.get("STATIC_DIR", Path(__file__).resolve().parents[2] / "frontend" / "dist"))
ALLOWED_HOSTS = [h.strip() for h in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h.strip()]
ALLOWED_ORIGINS = {o.strip().rstrip("/").lower() for o in os.environ.get("ALLOWED_ORIGINS", "").split(",") if o.strip()}

app = FastAPI(title="HTML Notebook")


def reject(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def origin_allowed(origin: str, host: str | None) -> bool:
    norm = origin.strip().rstrip("/").lower()
    netloc = urlsplit(norm).netloc
    if host and netloc == host.lower():
        return True
    return any(fnmatchcase(norm, pat) or fnmatchcase(netloc, pat) for pat in ALLOWED_ORIGINS)


@app.middleware("http")
async def protect(request: Request, call_next):
    if request.method in UNSAFE_METHODS:
        origin = request.headers.get("origin")
        if origin and not origin_allowed(origin, request.headers.get("host")):
            return reject(403, "Cross-site request blocked")
        if request.method in ("POST", "PUT") and not request.headers.get("content-type", "").startswith("application/json"):
            return reject(415, "Expected JSON")
        if not auth.csrf_valid(request):
            return reject(403, "Security token missing or expired. Reload the page and try again.")
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.add_middleware(GZipMiddleware, minimum_size=1000)


@app.exception_handler(StoreError)
async def store_error(_: Request, err: StoreError):
    return reject(err.status, err.message)


@app.exception_handler(FileNotFoundError)
async def not_found(_: Request, __: FileNotFoundError):
    return reject(404, "Not found")


app.include_router(auth.router)
app.include_router(storage.router)
app.include_router(share.router)


def user_store(user: User = Depends(auth.current_user), db: Session = Depends(get_db)) -> Store:
    return share.store_for_user(user.id, db)


class FolderIn(BaseModel):
    parent: str = ""
    name: str


class PageIn(BaseModel):
    section: str
    name: str
    html: str = ""


class SaveIn(BaseModel):
    html: str
    keepVersion: bool = False


class RenameIn(BaseModel):
    path: str
    name: str


@app.get("/healthz")
def health():
    return {"ok": True}


@app.get("/api/tree")
def get_tree(store: Store = Depends(user_store)):
    return store.tree()


@app.post("/api/folder")
def create_folder(body: FolderIn, store: Store = Depends(user_store)):
    return store.create_folder(body.parent, body.name)


@app.post("/api/page")
def create_page(body: PageIn, store: Store = Depends(user_store)):
    return store.create_page(body.section, body.name, body.html)


@app.put("/api/page")
def save_page(path: str, body: SaveIn, store: Store = Depends(user_store)):
    return store.write_page(path, body.html, body.keepVersion)


@app.post("/api/rename")
def rename(
    body: RenameIn,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
    store: Store = Depends(user_store),
):
    res = store.rename(body.path, body.name)
    share.sync_renamed_shares(user.id, body.path, res["path"], db)
    return res


@app.delete("/api/item")
def remove(
    path: str,
    user: User = Depends(auth.current_user),
    db: Session = Depends(get_db),
    store: Store = Depends(user_store),
):
    res = store.remove(path)
    share.delete_removed_shares(user.id, path, db)
    return res


@app.get("/api/versions")
def versions(path: str, store: Store = Depends(user_store)):
    return store.list_versions(path)


@app.get("/api/version")
def version(path: str, id: str, store: Store = Depends(user_store)):
    return store.read_version(path, id)


def page_response(html: str, csp: str) -> HTMLResponse:
    return HTMLResponse(html, headers={"Content-Security-Policy": csp, "Cache-Control": "no-store"})


@app.get("/page/{path:path}")
def edit_page(path: str, store: Store = Depends(user_store)):
    return page_response(store.read_page(path), EDIT_CSP)


@app.get("/view/{path:path}")
def view_page(path: str, store: Store = Depends(user_store)):
    return page_response(store.read_page(path), VIEW_CSP)


@app.get("/s/{token}")
def shared_page(token: str, db: Session = Depends(get_db)):
    html = share.public_share(token, db)
    if html is None:
        return HTMLResponse(
            "<!DOCTYPE html><html><body style='font-family:system-ui;padding:48px;color:#475467'><h2>Shared note not found or expired</h2></body></html>",
            status_code=404,
            headers={"Content-Security-Policy": VIEW_CSP, "Cache-Control": "no-store"},
        )
    return page_response(html, VIEW_CSP)


if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="frontend")
