# PST Extractor & Hybrid Search

Upload Outlook `.pst` / `.ost` archives, extract every email and attachment, and search them
with hybrid keyword + semantic search. One container, one volume, no external services:
PostgreSQL 16 + pgvector runs inside the container on `127.0.0.1` and stores its data under
`/data`. See [REVIEW.md](REVIEW.md) for what changed from the original spec and why.

```
browser ──► FastAPI (uvicorn, 1 process)
             ├─ chunked upload ──► /data/uploads/{archive_id}.pst
             ├─ JobDispatcher ── claims QUEUED archives (FOR UPDATE SKIP LOCKED)
             │     └─ ProcessPoolExecutor (spawn) ─► pypff → bodies + attachments
             │                                      → text extraction → chunks → embeddings
             │                                      → Postgres (COPY) + /data/attachments/…
             └─ /api/search ── RRF( GIN full-text , HNSW cosine ) ─► results per email/attachment
            local PostgreSQL 16 + pgvector  (data dir /data/pgdata, started by scripts/ensure_local_db.sh)
```

## Run locally

Needs Python 3.11+ and either root/sudo on Debian/Ubuntu or Homebrew on macOS (the bootstrap
script installs PostgreSQL 16 + pgvector if they are missing).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --port 8000        # first start initialises the database, then open http://localhost:8000
```

On startup the app runs `scripts/ensure_local_db.sh`, which installs Postgres + pgvector if
needed, initialises a cluster in `$DATA_DIR/pgdata` (default `/data`, or `./data` when `/data`
is not writable), starts it on `127.0.0.1:$PG_PORT`, and creates the role, database and
extensions. Then the schema in `sql/schema.sql` is applied. Shutting the app down stops the
server again if the app started it. You can also run the script by hand:
`bash scripts/ensure_local_db.sh` / `bash scripts/ensure_local_db.sh stop`.

Index a file without the UI, or search from the terminal:

```bash
python -m app.cli ingest ~/Downloads/archive.pst
python -m app.cli search "gas imbalance statement" --mode hybrid
```

## Deploy on Railway

1. Create a service from this repository. `railway.toml` selects the Dockerfile and the
   `/healthz` health check.
2. Attach a **volume mounted at `/data`**. It holds the Postgres cluster, uploads and
   attachments.
3. Set `APP_PASSWORD` (the UI and API then require HTTP Basic auth, user `admin` by default).
   Without it, anyone with the URL can read the mailboxes.
4. Keep the service at **one replica**. The volume holds a live database cluster.

The image installs Postgres and pgvector at build time (official PGDG repository, pgvector 0.8)
and bakes in the `bge-small-en-v1.5` model, so a restart never touches apt or downloads a model.
Size the service for the model (about 150 MB per worker process) plus Postgres.

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `/data` (or `./data`) | Root for `pgdata/`, `uploads/`, `attachments/`, `models/` |
| `APP_PASSWORD` / `APP_USER` | unset / `admin` | Enables HTTP Basic auth |
| `PG_PORT`, `PG_USER`, `PG_PASS`, `PG_DB` | `5432`, `pst_user`, `pst_local_pass`, `pst_search` | Local server and app role |
| `DATABASE_URL` | built from the `PG_*` values | Use an external Postgres (with pgvector) instead; bootstrap is then skipped |
| `BOOTSTRAP_DB` | `1` unless `DATABASE_URL` is set | Run `ensure_local_db.sh` on startup |
| `EMBEDDING_PROVIDER` | `fastembed` | `fastembed` (local), `openai`, or `hash` (offline tests only, lexical) |
| `EMBEDDING_MODEL` / `EMBED_DIM` | `BAAI/bge-small-en-v1.5` / 384 | `text-embedding-3-small` → 1536. The DB remembers the model and refuses a mismatch |
| `OPENAI_API_KEY` | | Required for `openai` |
| `WORKER_PROCESSES` | `1` | Parallel archives being indexed |
| `MAX_ATTACHMENT_MB` | `50` | Larger attachments are recorded but not stored or indexed |
| `CHUNK_CHARS` / `CHUNK_OVERLAP` | `1200` / `200` | Chunking for search |
| `UPLOAD_CHUNK_MB` | `10` | Upload chunk size |

## API

| Method & path | Purpose |
|---|---|
| `POST /api/uploads` `{filename, size}` | Start an upload; returns `id`, `chunk_size` |
| `PUT /api/uploads/{id}?offset=N` (raw bytes) | Write one chunk; contiguous, idempotent on retry |
| `GET /api/uploads/{id}` | Bytes received, for resuming |
| `POST /api/uploads/{id}/complete` | Verify size and PST signature, queue for indexing |
| `GET /api/archives`, `DELETE /api/archives/{id}`, `POST /api/archives/{id}/retry` | Manage archives |
| `GET /api/search?q=&mode=hybrid\|keyword\|semantic&archive_id=&sender=&date_from=&date_to=&source_type=` | Search |
| `GET /api/emails/{id}`, `GET /api/emails/{id}/html` | Email detail; HTML body served sandboxed with a strict CSP |
| `GET /api/attachments/{id}/download` | Original attachment (always as a download) |
| `GET /healthz`, `GET /api/stats` | Health and counts |

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q                                              # unit tests
PST_SAMPLE=path/to/file.pst pytest tests/test_parser.py
python tests/smoke_test.py path/to/file.pst --password "$APP_PASSWORD"   # end to end against a running server
```

## Limits

- Text only: no OCR for scanned PDFs or images. Legacy `.doc` is extracted best effort.
- Embedded messages (`.msg` attached inside a message) are recorded but not expanded.
- Password-protected or corrupt attachments are recorded with an `error:` extraction status.
