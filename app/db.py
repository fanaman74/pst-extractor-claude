"""Local database bootstrap (via scripts/ensure_local_db.sh) and schema migration."""
from __future__ import annotations

import logging
import os
import subprocess

from psycopg_pool import AsyncConnectionPool

from .config import PROJECT_ROOT, Settings

log = logging.getLogger("pst.db")

BOOTSTRAP_SCRIPT = PROJECT_ROOT / "scripts" / "ensure_local_db.sh"
SCHEMA_PATH = PROJECT_ROOT / "sql" / "schema.sql"


def _script_env(settings: Settings) -> dict:
    return {**os.environ, "DATA_DIR": settings.data_dir, "PG_PORT": str(settings.pg_port)}


def run_bootstrap(settings: Settings) -> bool:
    """Runs the idempotent bootstrap script. Returns True when it started the server."""
    log.info("Checking local PostgreSQL + pgvector (%s)", BOOTSTRAP_SCRIPT)
    proc = subprocess.run(
        ["bash", str(BOOTSTRAP_SCRIPT)], env=_script_env(settings), capture_output=True, text=True
    )
    for line in proc.stdout.splitlines():
        if line.startswith("["):
            log.info(line)
    if proc.returncode != 0:
        tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-25:])
        raise RuntimeError(f"Local database bootstrap failed (exit {proc.returncode}):\n{tail}")
    return "[DB-Started] 1" in proc.stdout


def stop_local_db(settings: Settings) -> None:
    subprocess.run(["bash", str(BOOTSTRAP_SCRIPT), "stop"], env=_script_env(settings), capture_output=True)


def render_schema(dim: int) -> str:
    return SCHEMA_PATH.read_text().replace("{{EMBED_DIM}}", str(int(dim)))


async def migrate(pool: AsyncConnectionPool, settings: Settings) -> None:
    async with pool.connection() as conn:
        await conn.execute(render_schema(settings.embed_dim))
        cur = await conn.execute("SELECT key, value FROM app_meta WHERE key IN ('embedding_model', 'embed_dim')")
        meta = dict(await cur.fetchall())
        want = {"embedding_model": settings.embedding_model, "embed_dim": str(settings.embed_dim)}
        if not meta:
            for k, v in want.items():
                await conn.execute("INSERT INTO app_meta (key, value) VALUES (%s, %s)", (k, v))
        elif meta.get("embed_dim") != want["embed_dim"] or meta.get("embedding_model") != want["embedding_model"]:
            raise RuntimeError(
                f"This database was indexed with {meta.get('embedding_model')} ({meta.get('embed_dim')} dims) "
                f"but the app is configured for {settings.embedding_model} ({settings.embed_dim} dims). "
                "Set EMBEDDING_PROVIDER / EMBEDDING_MODEL back, or start with a fresh database."
            )
    log.info("Schema ready (embeddings: %s, %d dims)", settings.embedding_model, settings.embed_dim)
