# Spec review: Self-Contained PST Extractor & Hybrid Search


What the spec asks for, what is wrong or risky in it, and what the build does instead.
The pasted spec was partly scrambled (the `psql` heredoc, `sql/schema.sql`, the
`lifespan` hook and the pipeline description were shuffled into a word list), so those
parts were reconstructed from the surviving tokens: `CREATE ROLE ... LOGIN PASSWORD`,
`SELECT ... FROM pg_database ... \gexec`, `CREATE EXTENSION vector; pg_trgm`,
tables `pst_archives / emails / attachments / search_chunks`, `fts_doc tsvector
GENERATED ALWAYS AS (to_tsvector('english', chunk_text)) STORED`, HNSW
`vector_cosine_ops`, `FOR UPDATE SKIP LOCKED`, `AsyncConnectionPool`, and the RRF query
that survived intact.

## What the spec gets right

- One container, one volume, no Redis: Postgres as the job queue (`FOR UPDATE SKIP LOCKED`)
  is a sound choice at this scale.
- Hybrid search with Reciprocal Rank Fusion (k = 60) over a GIN full‑text index and an
  HNSW vector index is the standard, robust approach.
- Local FastEmbed `bge-small-en-v1.5` (384 dims) keeps mailbox contents off third‑party APIs.
- Contextual chunks (prepending subject / sender / filename) measurably help retrieval.

## Problems found, and the fix in this build

### Database bootstrap (`ensure_local_db.sh`)

| # | Problem | Fix |
|---|---------|-----|
| 1 | Installing Postgres with `apt-get` at **runtime**. On Railway only `/data` persists, so every deploy/restart would re‑download and install Postgres (slow, needs network, can fail mid‑boot). | Postgres 16 + pgvector are installed in the **Dockerfile** at build time (Ubuntu 24.04 base + PGDG repo). The script still installs when binaries are missing, which only happens on a bare local machine. |
| 2 | `chown -R postgres:postgres /data` on every boot walks every upload and attachment (can be hundreds of GB) and hands app files to the `postgres` user. | Only `$PG_DATA_DIR` and the socket dir are chowned. |
| 3 | `dirname$(find ...)` is a syntax error (missing space); `find` across `/usr/bin` could pick up the wrong major version. | Bin dir resolved from `pg_config --bindir`, then `/usr/lib/postgresql/<newest>/bin`, then Homebrew prefixes. |
| 4 | `pg_config` is assumed to be on `PATH`; on Debian it lives in the versioned bin dir or `libpq-dev`. | Uses the resolved bin dir's `pg_config`. |
| 5 | After a container is killed, a stale `postmaster.pid` on the volume can block startup (PIDs get reused in containers). | If the server is not answering and no `postgres` process is running, the stale pid file is removed before `pg_ctl start`. |
| 6 | No clean shutdown, so every redeploy triggers crash recovery. | App shutdown runs `pg_ctl stop -m fast` when the app started the server itself. |
| 7 | `su - postgres -c "PATH=$PATH ..."` quoting breaks when values contain spaces/quotes; heredoc + `su -` loses stdin on some systems. | Commands run through a small `as_pg` helper (`runuser`/`su` with `-c` and properly quoted args, or directly when not root). SQL values go in as psql variables (`-v`) quoted with `format(%I, %L)`, so passwords with quotes are safe. |
| 8 | On macOS, `brew install` leaves no `postgres` OS user and `/data` is not writable. | Paths default to `./data` when `/data` is not writable; the script runs as the current user when there is no `postgres` user. |
| 9 | Data dir created by a different Postgres major version (image upgrade) crashes with a cryptic error. | Script compares `PG_VERSION` with the binary version and stops with a clear message. |
| 10 | Hard‑coded password `pst_local_pass`. | Server listens on `127.0.0.1` only, and the password is read from `PG_PASS` (default kept for local dev). |

### Ingestion pipeline

| # | Problem | Fix |
|---|---------|-----|
| 11 | `pip install pypff` installs an **unrelated package**. The libpff bindings are `libpff-python` (imported as `pypff`). | `requirements.txt` pins `libpff-python`. |
| 12 | pypff objects cannot be pickled, so they cannot cross a `ProcessPoolExecutor` boundary. | The worker process receives only `archive_id` + path, opens the PST itself and writes to Postgres with its own sync connection. |
| 13 | `ProcessPoolExecutor` with the default `fork` start method after the asyncio loop, the DB pool and ONNX threads exist can deadlock. | `spawn` context; embedding model loaded once per worker process. |
| 14 | A crash mid‑job leaves archives stuck in `PROCESSING` forever. | Jobs carry a heartbeat (`locked_at`); on startup and periodically, stale `PROCESSING` jobs go back to `QUEUED` (with an attempt counter, failing after 3). Re‑processing deletes partial rows first, so it is idempotent. |
| 15 | `get_sub_message()` returns non‑mail items (calendar, contacts, distribution lists) as `pypff.item`, which has no `.subject`; the naïve walker crashes. | Only `pypff.message` items are indexed; others are counted and skipped. Every message is wrapped in try/except so one corrupt item does not fail the archive. |
| 16 | Bodies come in three forms (plain, HTML, compressed RTF) and arbitrary encodings. | Plain → HTML stripped to text → RTF de‑formatted, with charset fallbacks. |
| 17 | Recipients / sender address / Message‑ID are not first‑class pypff attributes. | Read from MAPI properties (`PR_DISPLAY_TO`, recipient table `0x3001/0x39FE/0x3003/0x0C15`, `PR_SENDER_EMAIL_ADDRESS`, `PR_INTERNET_MESSAGE_ID`), falling back to transport headers. |
| 18 | `/data/attachments/{email_id}/{filename}` with a raw filename allows path traversal (`../../`) and collisions. | Filenames sanitized; stored as `{attachment_id}_{safe_name}`. |
| 19 | Only `.pdf/.docx/.xlsx/.csv/.txt` extracted. Real PSTs (e.g. the Enron test file) are full of legacy `.doc`/`.xls`, `.htm`, `.eml`/`.msg`. | Added `.xls` (xlrd), `.html/.htm`, `.md/.json/.xml/.eml`, and a best‑effort printable‑text fallback for legacy `.doc`. Per‑attachment size cap (`MAX_ATTACHMENT_MB`) and text cap so one huge file cannot stall a job. |
| 20 | Embedding dimension is hard‑coded `vector(384)` but OpenAI is offered at 1536. Switching models silently breaks inserts. | Dimension comes from config; schema is rendered with it and the chosen model is recorded in an `app_meta` table. Startup refuses a mismatch with a clear error. |
| 21 | Row‑by‑row inserts. | Batched: emails and chunks inserted per batch, embeddings computed in batches of 64. |

### Upload

| # | Problem | Fix |
|---|---------|-----|
| 22 | "Chunked upload" had no protocol: no offsets, no resume, no integrity check. | `POST /api/uploads` creates an upload, `PUT /api/uploads/{id}?offset=` writes each 10 MB chunk at its offset (idempotent, resumable), `GET` reports bytes received, `POST .../complete` checks size and the PST magic bytes `!BDN` before queueing. |
| 23 | Chunks were to be held in memory by the framework. | Chunks are streamed from the request into the file. |

### Search

| # | Problem | Fix |
|---|---------|-----|
| 24 | The spec's query uses `:name` placeholders, which psycopg does not accept. | `%(name)s` placeholders. |
| 25 | One long email can fill all results with its own chunks. | Results are grouped per email/attachment (best chunk wins) before the final limit. |
| 26 | No way to scope a search. | Optional filters: archive, sender, date range, source type. Applied in both branches; `hnsw.ef_search` raised when filtering. |
| 27 | `websearch_to_tsquery` with a query that is all stop words returns an empty tsquery and a notice. | Handled: keyword branch simply contributes nothing. |
| 28 | No highlighting. | `ts_headline` snippets, HTML-escaped, with matches wrapped in `<mark>`. |
| 29 | The spec's semantic CTE computes `ROW_NUMBER() OVER (ORDER BY embedding <=> q)` over the whole table, which forces a sequential scan and defeats the HNSW index. | Nearest neighbours are fetched in a subquery with `ORDER BY distance LIMIT 50` (index scan, confirmed with `EXPLAIN`), and ranked afterwards. |

### Security and operations

| # | Problem | Fix |
|---|---------|-----|
| 30 | **No authentication** on an app that exposes whole mailboxes on a public Railway URL. | HTTP Basic auth when `APP_PASSWORD` is set (the README says to set it on Railway). |
| 31 | FastEmbed downloads its model on first use; on Railway that would repeat on every deploy. | Model cache in `/data/models` (persisted) and optionally pre‑downloaded at image build. |
| 32 | Railway volumes attach to a single replica; horizontal scaling would corrupt the cluster. | Documented: run exactly one replica. |
| 33 | No delete. | `DELETE /api/archives/{id}` removes rows (cascade) and files. |
| 34 | No health check. | `GET /healthz` checks the DB; used by Railway. |

## Things deliberately kept simple

- UI is server‑rendered HTML + a small vanilla JS file (no Node build step), which keeps the
  single‑container story intact. The spec allowed Jinja2/HTMX.
- One worker process by default (`WORKER_PROCESSES=1`); each one holds its own ONNX model
  (~150 MB RAM).
- No OCR for scanned PDFs or images.

## How it was verified

- `scripts/ensure_local_db.sh` run on a clean Ubuntu 24.04 box: installed pgvector, ran
  `initdb`, started the server, created role/db/extensions; second run was a no‑op.
- A real 13 MB Enron PST (71 emails, 70 attachments: .doc, .xls, .pdf, images) uploaded
  through the chunked API, indexed, searched in all three modes with filters, email detail
  and attachment download checked, archive deleted (`tests/smoke_test.py`).
- Recovery: Postgres killed with `SIGKILL` (stale `postmaster.pid` cleaned up on next boot),
  an archive stuck in `PROCESSING` re‑queued and re‑indexed without duplicates, graceful
  shutdown stops the server.
- The Docker image built and ran with a named volume at `/data`; data survived `docker
  restart` and `docker kill`.
- 19 unit tests (`pytest`).
- Not verified here: the FastEmbed model download (Hugging Face was unreachable from the
  build sandbox, so tests used the offline `hash` embedder) and the PGDG apt repository in
  the Docker build (built with `USE_PGDG=0`, i.e. Ubuntu's pgvector 0.6).
