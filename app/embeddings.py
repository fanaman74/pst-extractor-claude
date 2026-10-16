"""Embedding providers: local FastEmbed (default), OpenAI, or a dependency-free hashing
embedder used for offline tests. All return L2-normalised float lists of `embed_dim`."""
from __future__ import annotations

import hashlib
import math
import re
from typing import Sequence

from .config import Settings

_TOKEN = re.compile(r"[a-z0-9]+")


class Embedder:
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        raise NotImplementedError

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


class FastEmbedEmbedder(Embedder):
    def __init__(self, settings: Settings):
        from fastembed import TextEmbedding  # imported lazily: heavy (onnxruntime)

        self.model = TextEmbedding(model_name=settings.embedding_model, cache_dir=settings.model_dir)
        self.dim = settings.embed_dim

    def embed_documents(self, texts):
        out = [v.tolist() for v in self.model.passage_embed(list(texts), batch_size=64)]
        _check_dim(out, self.dim)
        return out

    def embed_query(self, text):
        out = [v.tolist() for v in self.model.query_embed([text])]
        _check_dim(out, self.dim)
        return out[0]


class OpenAIEmbedder(Embedder):
    def __init__(self, settings: Settings):
        import httpx

        if not settings.openai_api_key:
            raise RuntimeError("EMBEDDING_PROVIDER=openai requires OPENAI_API_KEY")
        self.client = httpx.Client(
            base_url="https://api.openai.com/v1",
            headers={"Authorization": f"Bearer {settings.openai_api_key}"},
            timeout=120,
        )
        self.model = settings.embedding_model
        self.dim = settings.embed_dim

    def embed_documents(self, texts):
        out: list[list[float]] = []
        for i in range(0, len(texts), 256):
            batch = [t[:24000] or " " for t in texts[i : i + 256]]
            r = self.client.post("/embeddings", json={"model": self.model, "input": batch, "dimensions": self.dim})
            r.raise_for_status()
            out.extend(d["embedding"] for d in sorted(r.json()["data"], key=lambda d: d["index"]))
        return out


class HashEmbedder(Embedder):
    """Feature-hashing bag of words + bigrams. Deterministic and offline; lexical only, so it is
    for tests and air-gapped smoke runs, not real semantic search."""

    def __init__(self, settings: Settings):
        self.dim = settings.embed_dim

    def _one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        toks = _TOKEN.findall(text.lower())
        feats = toks + [a + " " + b for a, b in zip(toks, toks[1:])]
        for f in feats:
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=8).digest(), "little")
            vec[h % self.dim] += 1.0 if (h >> 63) & 1 else -1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def embed_documents(self, texts):
        return [self._one(t) for t in texts]


def _check_dim(vectors: list[list[float]], dim: int) -> None:
    if vectors and len(vectors[0]) != dim:
        raise RuntimeError(f"Model returned {len(vectors[0])}-dim vectors but EMBED_DIM={dim}")


def create_embedder(settings: Settings) -> Embedder:
    return {"fastembed": FastEmbedEmbedder, "openai": OpenAIEmbedder, "hash": HashEmbedder}[
        settings.embedding_provider
    ](settings)


def to_pgvector(vec: Sequence[float]) -> str:
    """Text literal accepted by `::vector` casts and COPY."""
    return "[" + ",".join(f"{v:.6g}" for v in vec) + "]"
