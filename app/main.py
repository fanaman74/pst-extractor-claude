"""FastAPI app: chunked PST upload, archive management, hybrid search, email viewer."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import secrets
import shutil
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from .config import PROJECT_ROOT, get_settings
from .db import migrate, run_bootstrap, stop_local_db
from .embeddings import create_embedder
from .extractors import safe_filename
from .search import SearchFilters, hybrid_search
from .worker import JobDispatcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("pst.app")

PST_MAGIC = b"!BDN"


class LazyEmbedder:
    """Loads the query-side model in the background so startup is not blocked by it."""

    def __init__(self, settings):
        self.settings = settings
        self._model = None
        self._lock = asyncio.Lock()

    async def get(self):
        async with self._lock:
            if self._model is None:
                self._model = await asyncio.to_thread(create_embedder, self.settings)
            return self._model


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    for d in (settings.upload_dir, settings.attachment_dir, settings.model_dir):
        os.makedirs(d, exist_ok=True)

    started_db = False
    if settings.bootstrap_db:
        started_db = await asyncio.to_thread(run_bootstrap, settings)

    pool = AsyncConnectionPool(conninfo=settings.database_url, min_size=1, max_size=10, open=False)
    await pool.open(wait=True, timeout=60)
    await migrate(pool, settings)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        app.state.pgvector_version = (await cur.fetchone())[0]

    app.state.settings = settings
    app.state.pool = pool
    app.state.embedder = LazyEmbedder(settings)
    app.state.dispatcher = JobDispatcher(pool, settings)
    await app.state.dispatcher.start()
    warm = asyncio.create_task(app.state.embedder.get())
    log.info("Ready. Data dir: %s", settings.data_dir)
    try:
        yield
    finally:
        warm.cancel()
        await app.state.dispatcher.stop()
        await pool.close()
        if started_db and settings.stop_db_on_exit:
            await asyncio.to_thread(stop_local_db, settings)


app = FastAPI(title="PST Extractor & Hybrid Search", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "app" / "static"), name="static")
templates = Jinja2Templates(directory=PROJECT_ROOT / "app" / "templates")
_basic = HTTPBasic(auto_error=False)


def require_auth(creds: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    s = get_settings()
    if not s.app_password:
        return
    ok = creds is not None and secrets.compare_digest(creds.username.encode(), s.app_user.encode()) \
        and secrets.compare_digest(creds.password.encode(), s.app_password.encode())
    if not ok:
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": 'Basic realm="pst"'})


def pool_of(request: Request) -> AsyncConnectionPool:
    return request.app.state.pool


async def fetch_one(pool, sql: str, params=()) -> dict | None:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return await cur.fetchone()


async def fetch_all(pool, sql: str, params=()) -> list[dict]:
    async with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(sql, params)
        return await cur.fetchall()


# ------------------------------------------------------------------------------------------
# Pages & health
# ------------------------------------------------------------------------------------------

@app.get("/healthz")
async def healthz(request: Request):
    await fetch_one(pool_of(request), "SELECT 1 AS ok")
    return {"ok": True}


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def index(request: Request):
    s = request.app.state.settings
    return templates.TemplateResponse(
        request, "index.html", {"chunk_mb": s.upload_chunk_mb, "model": s.embedding_model}
    )


# ------------------------------------------------------------------------------------------
# Chunked, resumable upload
# ------------------------------------------------------------------------------------------

class UploadCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=500)
    size: int = Field(gt=0)


@app.post("/api/uploads", dependencies=[Depends(require_auth)])
async def create_upload(body: UploadCreate, request: Request):
    s = request.app.state.settings
    if not body.filename.lower().endswith((".pst", ".ost")):
        raise HTTPException(400, "Only .pst and .ost files are accepted")
    free = shutil.disk_usage(s.upload_dir).free
    if body.size > free:
        raise HTTPException(507, f"Not enough disk space: {free} bytes free")
    row = await fetch_one(
        pool_of(request),
        """INSERT INTO pst_archives (filename, storage_path, file_size_bytes, status)
           VALUES (%s, '', %s, 'UPLOADING') RETURNING id""",
        (body.filename, body.size),
    )
    archive_id = str(row["id"])
    rel = os.path.join("uploads", f"{archive_id}.pst")
    open(os.path.join(s.data_dir, rel), "wb").close()
    await fetch_one(pool_of(request), "UPDATE pst_archives SET storage_path = %s WHERE id = %s RETURNING id", (rel, archive_id))
    return {"id": archive_id, "chunk_size": s.upload_chunk_mb * 1024 * 1024, "bytes_received": 0}


@app.get("/api/uploads/{archive_id}", dependencies=[Depends(require_auth)])
async def upload_status(archive_id: str, request: Request):
    row = await fetch_one(
        pool_of(request),
        "SELECT id, status, file_size_bytes, bytes_received FROM pst_archives WHERE id = %s",
        (_uuid(archive_id),),
    )
    if not row:
        raise HTTPException(404, "Unknown upload")
    return row


@app.put("/api/uploads/{archive_id}", dependencies=[Depends(require_auth)])
async def upload_chunk(archive_id: str, offset: int, request: Request):
    s = request.app.state.settings
    pool = pool_of(request)
    row = await fetch_one(
        pool, "SELECT status, storage_path, file_size_bytes, bytes_received FROM pst_archives WHERE id = %s",
        (_uuid(archive_id),),
    )
    if not row:
        raise HTTPException(404, "Unknown upload")
    if row["status"] != "UPLOADING":
        raise HTTPException(409, f"Upload is already {row['status']}")
    if offset < 0 or offset > row["bytes_received"]:
        raise HTTPException(409, f"Chunks must be contiguous; resume from {row['bytes_received']}")
    max_chunk = s.upload_chunk_mb * 1024 * 1024 * 2
    path = os.path.join(s.data_dir, row["storage_path"])
    written = 0
    # Stream the request body straight to disk at the given offset (idempotent on retry).
    fh = await asyncio.to_thread(open, path, "r+b")
    try:
        await asyncio.to_thread(fh.seek, offset)
        async for piece in request.stream():
            written += len(piece)
            if written > max_chunk or offset + written > row["file_size_bytes"]:
                raise HTTPException(413, "Chunk too large or beyond declared file size")
            await asyncio.to_thread(fh.write, piece)
    finally:
        await asyncio.to_thread(fh.close)
    upd = await fetch_one(
        pool,
        """UPDATE pst_archives SET bytes_received = GREATEST(bytes_received, %s)
           WHERE id = %s RETURNING bytes_received""",
        (offset + written, archive_id),
    )
    return {"bytes_received": upd["bytes_received"]}


@app.post("/api/uploads/{archive_id}/complete", dependencies=[Depends(require_auth)])
async def complete_upload(archive_id: str, request: Request):
    s = request.app.state.settings
    pool = pool_of(request)
    row = await fetch_one(pool, "SELECT * FROM pst_archives WHERE id = %s", (_uuid(archive_id),))
    if not row:
        raise HTTPException(404, "Unknown upload")
    if row["status"] != "UPLOADING":
        raise HTTPException(409, f"Upload is already {row['status']}")
    path = os.path.join(s.data_dir, row["storage_path"])
    size = os.path.getsize(path)
    if row["bytes_received"] != row["file_size_bytes"] or size != row["file_size_bytes"]:
        raise HTTPException(409, f"Incomplete upload: {row['bytes_received']} of {row['file_size_bytes']} bytes")
    with open(path, "rb") as fh:
        if fh.read(4) != PST_MAGIC:
            await fetch_one(
                pool, "UPDATE pst_archives SET status = 'FAILED', error_message = %s WHERE id = %s RETURNING id",
                ("Not a PST/OST file (missing !BDN signature)", archive_id),
            )
            raise HTTPException(400, "Not a PST/OST file (missing !BDN signature)")
    await fetch_one(pool, "UPDATE pst_archives SET status = 'QUEUED' WHERE id = %s RETURNING id", (archive_id,))
    request.app.state.dispatcher.notify()
    return {"id": archive_id, "status": "QUEUED"}


# ------------------------------------------------------------------------------------------
# Archives
# ------------------------------------------------------------------------------------------

@app.get("/api/archives", dependencies=[Depends(require_auth)])
async def list_archives(request: Request):
    return await fetch_all(
        pool_of(request),
        """SELECT id, filename, file_size_bytes, bytes_received, status, total_items, processed_items,
                  processed_emails, processed_attachments, skipped_items, attempts, error_message,
                  created_at, started_at, completed_at
           FROM pst_archives ORDER BY created_at DESC""",
    )


@app.delete("/api/archives/{archive_id}", dependencies=[Depends(require_auth)])
async def delete_archive(archive_id: str, request: Request):
    s = request.app.state.settings
    row = await fetch_one(
        pool_of(request), "DELETE FROM pst_archives WHERE id = %s RETURNING storage_path", (_uuid(archive_id),)
    )
    if not row:
        raise HTTPException(404, "Unknown archive")
    if row["storage_path"]:
        _remove(os.path.join(s.data_dir, row["storage_path"]))
    shutil.rmtree(os.path.join(s.attachment_dir, archive_id), ignore_errors=True)
    return {"deleted": archive_id}


@app.post("/api/archives/{archive_id}/retry", dependencies=[Depends(require_auth)])
async def retry_archive(archive_id: str, request: Request):
    row = await fetch_one(
        pool_of(request),
        """UPDATE pst_archives SET status = 'QUEUED', attempts = 0, error_message = NULL
           WHERE id = %s AND status IN ('FAILED', 'COMPLETED') AND bytes_received = file_size_bytes RETURNING id""",
        (_uuid(archive_id),),
    )
    if not row:
        raise HTTPException(409, "Only failed or completed, fully uploaded archives can be re-processed")
    request.app.state.dispatcher.notify()
    return {"id": archive_id, "status": "QUEUED"}


# ------------------------------------------------------------------------------------------
# Search & viewing
# ------------------------------------------------------------------------------------------

@app.get("/api/search", dependencies=[Depends(require_auth)])
async def search(
    request: Request, q: str, mode: str = "hybrid", limit: int = 20, archive_id: str | None = None,
    sender: str | None = None, date_from: dt.date | None = None, date_to: dt.date | None = None,
    source_type: str | None = None,
):
    filters = SearchFilters(
        archive_id=_uuid(archive_id) if archive_id else None, sender=sender or None,
        date_from=date_from, date_to=date_to, source_type=source_type or None,
    )
    embedder = await request.app.state.embedder.get() if mode != "keyword" else None
    results = await hybrid_search(
        pool_of(request), embedder, q, mode=mode, limit=limit, filters=filters,
        pgvector_version=request.app.state.pgvector_version,
    )
    return {"query": q, "mode": mode, "count": len(results), "results": results}


@app.get("/api/emails/{email_id}", dependencies=[Depends(require_auth)])
async def get_email(email_id: str, request: Request):
    pool = pool_of(request)
    email = await fetch_one(
        pool,
        """SELECT e.id, e.archive_id, p.filename AS archive_filename, e.folder_path, e.internet_message_id,
                  e.subject, e.sender_name, e.sender_email, e.recipients, e.sent_at, e.headers, e.body_text,
                  (e.body_html IS NOT NULL AND e.body_html <> '') AS has_html
           FROM emails e JOIN pst_archives p ON p.id = e.archive_id WHERE e.id = %s""",
        (_uuid(email_id),),
    )
    if not email:
        raise HTTPException(404, "Unknown email")
    email["attachments"] = await fetch_all(
        pool,
        """SELECT id, filename, mime_type, size_bytes, extraction_status, storage_path IS NOT NULL AS downloadable
           FROM attachments WHERE email_id = %s ORDER BY filename""",
        (email_id,),
    )
    return email


@app.get("/api/emails/{email_id}/html", dependencies=[Depends(require_auth)], response_class=HTMLResponse)
async def get_email_html(email_id: str, request: Request):
    row = await fetch_one(pool_of(request), "SELECT body_html FROM emails WHERE id = %s", (_uuid(email_id),))
    if not row or not row["body_html"]:
        raise HTTPException(404, "No HTML body")
    # Served into a sandboxed iframe; CSP blocks scripts and remote loads (tracking pixels).
    return HTMLResponse(
        row["body_html"],
        headers={"Content-Security-Policy": "default-src 'none'; img-src data:; style-src 'unsafe-inline'; sandbox"},
    )


@app.get("/api/attachments/{attachment_id}/download", dependencies=[Depends(require_auth)])
async def download_attachment(attachment_id: str, request: Request):
    s = request.app.state.settings
    row = await fetch_one(
        pool_of(request), "SELECT filename, storage_path FROM attachments WHERE id = %s", (_uuid(attachment_id),)
    )
    if not row or not row["storage_path"]:
        raise HTTPException(404, "Attachment content not available")
    path = os.path.realpath(os.path.join(s.data_dir, row["storage_path"]))
    if not path.startswith(os.path.realpath(s.attachment_dir) + os.sep) or not os.path.exists(path):
        raise HTTPException(404, "Attachment file missing")
    # Always a download, never rendered inline (attachments may contain active HTML/SVG).
    return FileResponse(path, filename=safe_filename(row["filename"]), media_type="application/octet-stream")


@app.get("/api/stats", dependencies=[Depends(require_auth)])
async def stats(request: Request):
    return await fetch_one(
        pool_of(request),
        """SELECT (SELECT count(*) FROM pst_archives) AS archives, (SELECT count(*) FROM emails) AS emails,
                  (SELECT count(*) FROM attachments) AS attachments, (SELECT count(*) FROM search_chunks) AS chunks""",
    )


def _uuid(value: str) -> str:
    import uuid

    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError):
        raise HTTPException(404, "Not found")


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
