"""Command line helpers, mainly for local use and smoke tests.

    python -m app.cli ingest path/to/archive.pst     # index a PST without the web UI
    python -m app.cli search "pipeline agreement"    # run a hybrid search
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil

from psycopg_pool import AsyncConnectionPool

from .config import get_settings
from .db import migrate, run_bootstrap
from .embeddings import create_embedder
from .search import hybrid_search
from .worker import process_archive


async def _pool(settings) -> AsyncConnectionPool:
    if settings.bootstrap_db:
        await asyncio.to_thread(run_bootstrap, settings)
    pool = AsyncConnectionPool(settings.database_url, min_size=1, max_size=2, open=False)
    await pool.open(wait=True)
    await migrate(pool, settings)
    return pool


async def ingest(path: str) -> None:
    settings = get_settings()
    pool = await _pool(settings)
    size = os.path.getsize(path)
    async with pool.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO pst_archives (filename, storage_path, file_size_bytes, bytes_received, status, attempts, locked_at)
               VALUES (%s, '', %s, %s, 'PROCESSING', 1, NOW()) RETURNING id""",
            (os.path.basename(path), size, size),
        )
        archive_id = str((await cur.fetchone())[0])
        rel = os.path.join("uploads", f"{archive_id}.pst")
        os.makedirs(settings.upload_dir, exist_ok=True)
        shutil.copyfile(path, os.path.join(settings.data_dir, rel))
        await conn.execute("UPDATE pst_archives SET storage_path = %s WHERE id = %s", (rel, archive_id))
    await pool.close()
    status = await asyncio.to_thread(process_archive, archive_id, settings.as_dict())
    print(f"{archive_id}: {status}")


async def search(query: str, mode: str, limit: int) -> None:
    settings = get_settings()
    pool = await _pool(settings)
    async with pool.connection() as conn:
        cur = await conn.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        version = (await cur.fetchone())[0]
    embedder = create_embedder(settings) if mode != "keyword" else None
    rows = await hybrid_search(pool, embedder, query, mode=mode, limit=limit, pgvector_version=version)
    for r in rows:
        where = f"📎 {r['attachment_filename']}" if r["attachment_filename"] else "body"
        print(f"{r['rrf_score']:.4f}  kw={r['kw_rank']} sem={r['sem_rank']}  {r['subject']!r} [{where}] {r['sender_email']}")
    await pool.close()


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m app.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest")
    p.add_argument("path")
    s = sub.add_parser("search")
    s.add_argument("query")
    s.add_argument("--mode", default="hybrid", choices=["hybrid", "keyword", "semantic"])
    s.add_argument("--limit", type=int, default=10)
    args = ap.parse_args()
    if args.cmd == "ingest":
        asyncio.run(ingest(args.path))
    else:
        asyncio.run(search(args.query, args.mode, args.limit))


if __name__ == "__main__":
    main()
