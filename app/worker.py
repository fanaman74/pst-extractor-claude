"""Background ingestion.

The web process runs a `JobDispatcher` that claims QUEUED archives from Postgres with
`FOR UPDATE SKIP LOCKED` and hands each one to a `spawn`-context ProcessPoolExecutor.
`process_archive` runs inside the worker process: it opens the PST itself (pypff objects
cannot be pickled), writes with its own sync connection, and heartbeats `locked_at`.
"""
from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import shutil
import uuid
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from .chunking import chunk_text
from .config import Settings
from .embeddings import Embedder, create_embedder, to_pgvector
from .extractors import extract_attachment_text, safe_filename
from .pst_parser import ParsedEmail, iter_messages, open_and_count

log = logging.getLogger("pst.worker")

BATCH_EMAILS = 25
STALE_AFTER = "15 minutes"
MAX_ATTEMPTS = 3

_embedder: Embedder | None = None


class ArchiveGone(Exception):
    """The archive row was deleted or reset while we were processing it."""


def _clean(s: str | None, limit: int | None = None) -> str | None:
    if s is None:
        return None
    s = s.replace("\x00", "")
    return s[:limit] if limit else s


def _get_embedder(settings: Settings) -> Embedder:
    global _embedder
    if _embedder is None:
        _embedder = create_embedder(settings)
    return _embedder


# --------------------------------------------------------------------------------------------
# Worker-process side
# --------------------------------------------------------------------------------------------

def process_archive(archive_id: str, settings_dict: dict) -> str:
    """Entry point executed in a worker process. Returns the final status."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [worker] %(message)s")
    settings = Settings(**settings_dict)
    with psycopg.connect(settings.database_url) as conn:
        try:
            _ingest(conn, archive_id, settings)
            return "COMPLETED"
        except ArchiveGone:
            conn.rollback()
            if not conn.execute("SELECT 1 FROM pst_archives WHERE id = %s", (archive_id,)).fetchone():
                shutil.rmtree(os.path.join(settings.attachment_dir, archive_id), ignore_errors=True)
            log.info("Archive %s was deleted or reset during processing; stopping", archive_id)
            return "CANCELLED"
        except Exception as exc:
            conn.rollback()
            if not conn.execute("SELECT 1 FROM pst_archives WHERE id = %s", (archive_id,)).fetchone():
                shutil.rmtree(os.path.join(settings.attachment_dir, archive_id), ignore_errors=True)
                log.info("Archive %s was deleted during processing; stopping", archive_id)
                return "CANCELLED"
            log.exception("Archive %s failed", archive_id)
            conn.execute(
                "UPDATE pst_archives SET status = 'FAILED', error_message = %s, locked_at = NULL WHERE id = %s",
                (f"{type(exc).__name__}: {exc}"[:2000], archive_id),
            )
            conn.commit()
            return "FAILED"


def _ingest(conn: psycopg.Connection, archive_id: str, settings: Settings) -> None:
    row = conn.execute("SELECT storage_path FROM pst_archives WHERE id = %s", (archive_id,)).fetchone()
    if not row:
        raise ArchiveGone()
    pst_path = os.path.join(settings.data_dir, row[0])

    # Idempotent restart: remove anything a previous, interrupted attempt wrote.
    conn.execute("DELETE FROM emails WHERE archive_id = %s", (archive_id,))
    shutil.rmtree(os.path.join(settings.attachment_dir, archive_id), ignore_errors=True)
    total = open_and_count(pst_path)
    conn.execute(
        """UPDATE pst_archives SET total_items = %s, processed_items = 0, processed_emails = 0,
               processed_attachments = 0, skipped_items = 0, locked_at = NOW() WHERE id = %s""",
        (total, archive_id),
    )
    conn.commit()
    log.info("Archive %s: %d items to process", archive_id, total)

    embedder = _get_embedder(settings)
    counters = {"items": 0, "emails": 0, "attachments": 0, "skipped": 0}
    batch: list[ParsedEmail] = []
    max_bytes = settings.max_attachment_mb * 1024 * 1024

    for email, skip_reason in iter_messages(pst_path, max_bytes):
        counters["items"] += 1
        if email is None:
            counters["skipped"] += 1
            continue
        batch.append(email)
        if len(batch) >= BATCH_EMAILS:
            _flush(conn, archive_id, batch, settings, embedder, counters)
            batch = []
    _flush(conn, archive_id, batch, settings, embedder, counters)

    conn.execute(
        """UPDATE pst_archives SET status = 'COMPLETED', completed_at = NOW(), locked_at = NULL,
               error_message = NULL WHERE id = %s""",
        (archive_id,),
    )
    conn.commit()
    log.info("Archive %s completed: %s", archive_id, counters)


def _flush(conn, archive_id: str, batch: list[ParsedEmail], settings: Settings, embedder: Embedder, counters) -> None:
    email_rows, attachment_rows, chunk_rows, embed_inputs = [], [], [], []

    for em in batch:
        email_id = str(uuid.uuid4())
        sender = f"{em.sender_name} <{em.sender_email}>" if em.sender_email else em.sender_name
        context = f"Subject: {em.subject}\nFrom: {sender}"
        email_rows.append((
            email_id, archive_id, _clean(em.folder_path), _clean(em.internet_message_id), _clean(em.subject),
            _clean(em.sender_name), _clean(em.sender_email).lower() if em.sender_email else None,
            Jsonb([{k: _clean(v) if isinstance(v, str) else v for k, v in r.items()} for r in em.recipients]),
            em.sent_at, _clean(em.headers, 100_000), _clean(em.body_text, settings.max_text_chars),
            _clean(em.body_html, 2_000_000), bool(em.attachments),
        ))

        body_chunks = chunk_text(_clean(em.body_text) or "", settings.chunk_chars, settings.chunk_overlap)
        if not body_chunks:
            body_chunks = [em.subject or "(no subject, no body)"]
        for i, ch in enumerate(body_chunks):
            chunk_rows.append([archive_id, email_id, None, "EMAIL_BODY", i, context, ch, em.sent_at, em.sender_email.lower() or None])
            embed_inputs.append(f"{context}\n\n{ch}")

        for att in em.attachments:
            att_id = str(uuid.uuid4())
            fname = safe_filename(att.filename)
            rel_path, text, status = None, "", att.note or "no data"
            if att.data is not None:
                rel_dir = os.path.join("attachments", archive_id, email_id)
                os.makedirs(os.path.join(settings.data_dir, rel_dir), exist_ok=True)
                rel_path = os.path.join(rel_dir, f"{att_id[:8]}_{fname}")
                with open(os.path.join(settings.data_dir, rel_path), "wb") as fh:
                    fh.write(att.data)
                text, status = extract_attachment_text(fname, att.data, settings.max_text_chars)
            attachment_rows.append((
                att_id, email_id, archive_id, _clean(att.filename, 500), _clean(att.mime_type), att.size,
                rel_path, _clean(text) or None, status,
            ))
            counters["attachments"] += 1
            if text:
                att_context = f"[Attachment: {fname}] {context}"
                for i, ch in enumerate(chunk_text(_clean(text), settings.chunk_chars, settings.chunk_overlap)):
                    chunk_rows.append([archive_id, email_id, att_id, "ATTACHMENT", i, att_context, ch, em.sent_at, em.sender_email.lower() or None])
                    embed_inputs.append(f"{att_context}\n\n{ch}")
        counters["emails"] += 1

    vectors = embedder.embed_documents(embed_inputs) if embed_inputs else []
    with conn.cursor() as cur:
        if email_rows:
            cur.executemany(
                """INSERT INTO emails (id, archive_id, folder_path, internet_message_id, subject, sender_name,
                       sender_email, recipients, sent_at, headers, body_text, body_html, has_attachments)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                email_rows,
            )
        if attachment_rows:
            cur.executemany(
                """INSERT INTO attachments (id, email_id, archive_id, filename, mime_type, size_bytes,
                       storage_path, extracted_text, extraction_status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                attachment_rows,
            )
        if chunk_rows:
            with cur.copy(
                """COPY search_chunks (archive_id, email_id, attachment_id, source_type, chunk_index, context,
                       chunk_text, sent_at, sender_email, embedding) FROM STDIN"""
            ) as copy:
                for row, vec in zip(chunk_rows, vectors):
                    copy.write_row(row + [to_pgvector(vec)])
        cur.execute(
            """UPDATE pst_archives SET processed_items = %s, processed_emails = %s, processed_attachments = %s,
                   skipped_items = %s, locked_at = NOW()
               WHERE id = %s AND status = 'PROCESSING'""",
            (counters["items"], counters["emails"], counters["attachments"], counters["skipped"], archive_id),
        )
        if cur.rowcount == 0:
            raise ArchiveGone()
    conn.commit()


# --------------------------------------------------------------------------------------------
# Web-process side
# --------------------------------------------------------------------------------------------

class JobDispatcher:
    def __init__(self, pool: AsyncConnectionPool, settings: Settings):
        self.pool = pool
        self.settings = settings
        self.wake = asyncio.Event()
        self.running: dict[str, asyncio.Task] = {}
        self._task: asyncio.Task | None = None
        self._executor: ProcessPoolExecutor | None = None

    def _new_executor(self) -> ProcessPoolExecutor:
        # spawn: forking a process that already runs an event loop, a DB pool and ONNX threads is unsafe
        return ProcessPoolExecutor(
            max_workers=self.settings.worker_processes, mp_context=multiprocessing.get_context("spawn")
        )

    async def start(self) -> None:
        await self.requeue_stale(all_processing=True)  # single instance: anything PROCESSING is orphaned
        self._executor = self._new_executor()
        self._task = asyncio.create_task(self._loop(), name="job-dispatcher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self._executor:
            procs = list(getattr(self._executor, "_processes", {}).values())
            self._executor.shutdown(wait=False, cancel_futures=True)
            for p in procs:  # interrupted jobs are re-queued on next start
                if p.is_alive():
                    p.terminate()

    def notify(self) -> None:
        self.wake.set()

    async def requeue_stale(self, all_processing: bool = False) -> None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                f"""UPDATE pst_archives
                       SET status = CASE WHEN attempts >= %s THEN 'FAILED' ELSE 'QUEUED' END,
                           error_message = CASE WHEN attempts >= %s THEN 'Gave up after repeated interruptions'
                                                ELSE error_message END,
                           locked_at = NULL
                     WHERE status = 'PROCESSING'
                       AND (%s OR locked_at < NOW() - INTERVAL '{STALE_AFTER}')
                       AND NOT (id = ANY(%s::uuid[]))
                 RETURNING id""",
                (MAX_ATTEMPTS, MAX_ATTEMPTS, all_processing, list(self.running)),
            )
            for (aid,) in await cur.fetchall():
                log.warning("Re-queued stale archive %s", aid)

    async def _claim(self) -> str | None:
        async with self.pool.connection() as conn:
            cur = await conn.execute(
                """UPDATE pst_archives
                       SET status = 'PROCESSING', locked_at = NOW(), started_at = NOW(),
                           attempts = attempts + 1, error_message = NULL
                     WHERE id = (SELECT id FROM pst_archives WHERE status = 'QUEUED'
                                  ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1)
                 RETURNING id"""
            )
            row = await cur.fetchone()
            return str(row[0]) if row else None

    async def _loop(self) -> None:
        while True:
            try:
                await self.requeue_stale()
                while len(self.running) < self.settings.worker_processes:
                    archive_id = await self._claim()
                    if not archive_id:
                        break
                    log.info("Dispatching archive %s", archive_id)
                    self.running[archive_id] = asyncio.create_task(self._run(archive_id))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Dispatcher iteration failed")
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()

    async def _run(self, archive_id: str) -> None:
        loop = asyncio.get_running_loop()
        try:
            status = await loop.run_in_executor(self._executor, process_archive, archive_id, self.settings.as_dict())
            log.info("Archive %s finished: %s", archive_id, status)
        except BrokenProcessPool:
            log.error("Worker process died while processing %s (out of memory?)", archive_id)
            self._executor = self._new_executor()
            async with self.pool.connection() as conn:
                await conn.execute(
                    """UPDATE pst_archives SET status = CASE WHEN attempts >= %s THEN 'FAILED' ELSE 'QUEUED' END,
                           error_message = 'Worker process crashed', locked_at = NULL
                       WHERE id = %s AND status = 'PROCESSING'""",
                    (MAX_ATTEMPTS, archive_id),
                )
        except Exception as exc:
            log.exception("Archive %s failed in dispatcher", archive_id)
            async with self.pool.connection() as conn:
                await conn.execute(
                    "UPDATE pst_archives SET status = 'FAILED', error_message = %s, locked_at = NULL WHERE id = %s",
                    (f"{type(exc).__name__}: {exc}"[:2000], archive_id),
                )
        finally:
            self.running.pop(archive_id, None)
            self.wake.set()
