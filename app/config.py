"""Runtime configuration, read once from environment variables."""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Known dimensions; any other model needs EMBED_DIM set explicitly.
MODEL_DIMS = {
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
}
DEFAULT_MODELS = {"fastembed": "BAAI/bge-small-en-v1.5", "openai": "text-embedding-3-small", "hash": "hash-v1"}


def _default_data_dir() -> str:
    try:
        os.makedirs("/data", exist_ok=True)
        if os.access("/data", os.W_OK):
            return "/data"
    except OSError:
        pass
    return str(PROJECT_ROOT / "data")


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    data_dir: str
    database_url: str
    bootstrap_db: bool
    stop_db_on_exit: bool
    pg_port: int
    embedding_provider: str
    embedding_model: str
    embed_dim: int
    openai_api_key: str
    worker_processes: int
    app_user: str
    app_password: str
    max_attachment_mb: int
    max_text_chars: int
    chunk_chars: int
    chunk_overlap: int
    upload_chunk_mb: int

    @property
    def upload_dir(self) -> str:
        return os.path.join(self.data_dir, "uploads")

    @property
    def attachment_dir(self) -> str:
        return os.path.join(self.data_dir, "attachments")

    @property
    def model_dir(self) -> str:
        return os.environ.get("FASTEMBED_CACHE_PATH") or os.path.join(self.data_dir, "models")

    def as_dict(self) -> dict:
        return asdict(self)


@lru_cache
def get_settings() -> Settings:
    data_dir = os.environ.get("DATA_DIR") or _default_data_dir()
    pg_port = int(os.environ.get("PG_PORT", "5432"))
    explicit_url = os.environ.get("DATABASE_URL")
    database_url = explicit_url or "postgresql://{u}:{p}@127.0.0.1:{port}/{db}".format(
        u=os.environ.get("PG_USER", "pst_user"),
        p=os.environ.get("PG_PASS", "pst_local_pass"),
        port=pg_port,
        db=os.environ.get("PG_DB", "pst_search"),
    )
    provider = os.environ.get("EMBEDDING_PROVIDER", "fastembed").lower()
    if provider not in DEFAULT_MODELS:
        raise ValueError(f"EMBEDDING_PROVIDER must be one of {sorted(DEFAULT_MODELS)}")
    model = os.environ.get("EMBEDDING_MODEL") or DEFAULT_MODELS[provider]
    dim = int(os.environ.get("EMBED_DIM") or MODEL_DIMS.get(model, 384))
    return Settings(
        data_dir=data_dir,
        database_url=database_url,
        # With an external DATABASE_URL we do not manage a local server by default.
        bootstrap_db=_bool("BOOTSTRAP_DB", explicit_url is None),
        stop_db_on_exit=_bool("STOP_DB_ON_EXIT", True),
        pg_port=pg_port,
        embedding_provider=provider,
        embedding_model=model,
        embed_dim=dim,
        openai_api_key=os.environ.get("OPENAI_API_KEY", ""),
        worker_processes=max(1, int(os.environ.get("WORKER_PROCESSES", "1"))),
        app_user=os.environ.get("APP_USER", "admin"),
        app_password=os.environ.get("APP_PASSWORD", ""),
        max_attachment_mb=int(os.environ.get("MAX_ATTACHMENT_MB", "50")),
        max_text_chars=int(os.environ.get("MAX_TEXT_CHARS", "300000")),
        chunk_chars=int(os.environ.get("CHUNK_CHARS", "1200")),
        chunk_overlap=int(os.environ.get("CHUNK_OVERLAP", "200")),
        upload_chunk_mb=int(os.environ.get("UPLOAD_CHUNK_MB", "10")),
    )
