-- Idempotent schema. {{EMBED_DIM}} is replaced by the configured embedding dimension.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

CREATE TABLE IF NOT EXISTS app_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per uploaded archive; doubles as the job queue (FOR UPDATE SKIP LOCKED).
CREATE TABLE IF NOT EXISTS pst_archives (
    id                    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    filename              TEXT NOT NULL,
    storage_path          TEXT NOT NULL,
    file_size_bytes       BIGINT NOT NULL DEFAULT 0,
    bytes_received        BIGINT NOT NULL DEFAULT 0,
    status                TEXT NOT NULL DEFAULT 'UPLOADING'
                          CHECK (status IN ('UPLOADING', 'QUEUED', 'PROCESSING', 'COMPLETED', 'FAILED')),
    total_items           INT,
    processed_items       INT NOT NULL DEFAULT 0,
    processed_emails      INT NOT NULL DEFAULT 0,
    processed_attachments INT NOT NULL DEFAULT 0,
    skipped_items         INT NOT NULL DEFAULT 0,
    attempts              INT NOT NULL DEFAULT 0,
    locked_at             TIMESTAMPTZ,
    error_message         TEXT,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at            TIMESTAMPTZ,
    completed_at          TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_pst_archives_queue ON pst_archives (status, created_at);

CREATE TABLE IF NOT EXISTS emails (
    id                  UUID PRIMARY KEY,
    archive_id          UUID NOT NULL REFERENCES pst_archives(id) ON DELETE CASCADE,
    folder_path         TEXT NOT NULL DEFAULT '',
    internet_message_id TEXT,
    subject             TEXT,
    sender_name         TEXT,
    sender_email        TEXT,
    recipients          JSONB NOT NULL DEFAULT '[]'::jsonb,
    sent_at             TIMESTAMPTZ,
    headers             TEXT,
    body_text           TEXT,
    body_html           TEXT,
    has_attachments     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_emails_archive_id   ON emails (archive_id);
CREATE INDEX IF NOT EXISTS idx_emails_sent_at      ON emails (sent_at DESC);
CREATE INDEX IF NOT EXISTS idx_emails_sender_email ON emails (lower(sender_email));
CREATE INDEX IF NOT EXISTS idx_emails_subject_trgm ON emails USING GIN (subject gin_trgm_ops);

CREATE TABLE IF NOT EXISTS attachments (
    id                UUID PRIMARY KEY,
    email_id          UUID NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    archive_id        UUID NOT NULL REFERENCES pst_archives(id) ON DELETE CASCADE,
    filename          TEXT NOT NULL,
    mime_type         TEXT,
    size_bytes        BIGINT,
    storage_path      TEXT,
    extracted_text    TEXT,
    extraction_status TEXT NOT NULL DEFAULT 'pending',
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_attachments_email_id   ON attachments (email_id);
CREATE INDEX IF NOT EXISTS idx_attachments_archive_id ON attachments (archive_id);

-- Unified search table: email body chunks and attachment chunks.
-- `context` (subject / sender / filename) is embedded with the chunk and indexed at a lower weight.
CREATE TABLE IF NOT EXISTS search_chunks (
    id            BIGSERIAL PRIMARY KEY,
    archive_id    UUID NOT NULL REFERENCES pst_archives(id) ON DELETE CASCADE,
    email_id      UUID NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
    attachment_id UUID REFERENCES attachments(id) ON DELETE CASCADE,
    source_type   TEXT NOT NULL CHECK (source_type IN ('EMAIL_BODY', 'ATTACHMENT')),
    chunk_index   INT NOT NULL,
    context       TEXT NOT NULL DEFAULT '',
    chunk_text    TEXT NOT NULL,
    sent_at       TIMESTAMPTZ,
    sender_email  TEXT,
    embedding     vector({{EMBED_DIM}}) NOT NULL,
    fts_doc       tsvector GENERATED ALWAYS AS (
                      setweight(to_tsvector('english', coalesce(chunk_text, '')), 'A') ||
                      setweight(to_tsvector('english', coalesce(context, '')), 'C')
                  ) STORED
);
CREATE INDEX IF NOT EXISTS idx_search_chunks_fts        ON search_chunks USING GIN (fts_doc);
CREATE INDEX IF NOT EXISTS idx_search_chunks_embedding  ON search_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS idx_search_chunks_email_id   ON search_chunks (email_id);
CREATE INDEX IF NOT EXISTS idx_search_chunks_archive_id ON search_chunks (archive_id);
CREATE INDEX IF NOT EXISTS idx_search_chunks_attachment ON search_chunks (attachment_id);
