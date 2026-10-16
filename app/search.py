"""Hybrid search: full-text (GIN) + vector (HNSW) fused with Reciprocal Rank Fusion."""
from __future__ import annotations

import datetime as dt
import html
from dataclasses import dataclass

from psycopg.rows import dict_row

from .embeddings import to_pgvector

RRF_K = 60
CANDIDATES = 50
HL_START, HL_STOP = "\x02", "\x03"  # sentinels; escaped + turned into <mark> in Python


@dataclass
class SearchFilters:
    archive_id: str | None = None
    sender: str | None = None
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    source_type: str | None = None  # EMAIL_BODY | ATTACHMENT

    def sql(self, alias: str = "c") -> tuple[str, dict]:
        parts, params = [], {}
        if self.archive_id:
            parts.append(f"{alias}.archive_id = %(f_archive)s")
            params["f_archive"] = self.archive_id
        if self.sender:
            parts.append(f"{alias}.sender_email ILIKE %(f_sender)s")
            params["f_sender"] = f"%{self.sender.strip().lower()}%"
        if self.date_from:
            parts.append(f"{alias}.sent_at >= %(f_from)s")
            params["f_from"] = self.date_from
        if self.date_to:
            parts.append(f"{alias}.sent_at < %(f_to)s")
            params["f_to"] = self.date_to + dt.timedelta(days=1)
        if self.source_type in ("EMAIL_BODY", "ATTACHMENT"):
            parts.append(f"{alias}.source_type = %(f_source)s")
            params["f_source"] = self.source_type
        return ("".join(f" AND {p}" for p in parts), params)

    @property
    def active(self) -> bool:
        return any([self.archive_id, self.sender, self.date_from, self.date_to, self.source_type])


def build_query(mode: str, filters: SearchFilters) -> tuple[str, dict]:
    where, params = filters.sql("c")
    keyword = f"""
        keyword_search AS (
            SELECT c.id AS chunk_id,
                   ROW_NUMBER() OVER (ORDER BY ts_rank_cd(c.fts_doc, q.query) DESC, c.id) AS kw_rank
            FROM search_chunks c, (SELECT websearch_to_tsquery('english', %(q)s) AS query) q
            WHERE c.fts_doc @@ q.query {where}
            ORDER BY kw_rank
            LIMIT %(k)s
        )"""
    # The inner ORDER BY distance + LIMIT is what lets Postgres use the HNSW index;
    # ranking with ROW_NUMBER() over the whole table (as in the original spec) forces a full scan.
    semantic = f"""
        semantic_search AS (
            SELECT chunk_id, ROW_NUMBER() OVER (ORDER BY dist) AS sem_rank
            FROM (
                SELECT c.id AS chunk_id, c.embedding <=> %(emb)s::vector AS dist
                FROM search_chunks c
                WHERE TRUE {where}
                ORDER BY dist
                LIMIT %(k)s
            ) nn
        )"""
    empty_kw = "keyword_search AS (SELECT NULL::bigint AS chunk_id, NULL::bigint AS kw_rank WHERE FALSE)"
    empty_sem = "semantic_search AS (SELECT NULL::bigint AS chunk_id, NULL::bigint AS sem_rank WHERE FALSE)"
    ctes = [keyword if mode in ("hybrid", "keyword") else empty_kw, semantic if mode in ("hybrid", "semantic") else empty_sem]
    sql = f"""
    WITH {ctes[0]},
    {ctes[1]},
    rrf AS (
        SELECT COALESCE(k.chunk_id, s.chunk_id) AS chunk_id, k.kw_rank, s.sem_rank,
               COALESCE(1.0 / (%(rrf_k)s + k.kw_rank), 0.0) + COALESCE(1.0 / (%(rrf_k)s + s.sem_rank), 0.0) AS rrf_score
        FROM keyword_search k
        FULL OUTER JOIN semantic_search s ON k.chunk_id = s.chunk_id
    ),
    best AS (  -- one hit per email body / attachment: its best-scoring chunk
        SELECT DISTINCT ON (c.email_id, c.attachment_id)
               r.chunk_id, r.kw_rank, r.sem_rank, r.rrf_score, c.email_id, c.attachment_id, c.source_type, c.chunk_text
        FROM rrf r JOIN search_chunks c ON c.id = r.chunk_id
        ORDER BY c.email_id, c.attachment_id, r.rrf_score DESC
    )
    SELECT b.chunk_id, b.source_type, b.rrf_score, b.kw_rank, b.sem_rank,
           ts_headline('english', b.chunk_text, websearch_to_tsquery('english', %(q)s),
                       'StartSel=' || chr(2) || ', StopSel=' || chr(3) || ', MaxWords=40, MinWords=15, MaxFragments=2, FragmentDelimiter=" … "') AS snippet,
           e.id AS email_id, e.subject, e.sender_name, e.sender_email, e.sent_at, e.folder_path, e.archive_id,
           a.id AS attachment_id, a.filename AS attachment_filename
    FROM best b
    JOIN emails e ON e.id = b.email_id
    LEFT JOIN attachments a ON a.id = b.attachment_id
    ORDER BY b.rrf_score DESC
    LIMIT %(limit)s
    """
    return sql, params


def render_snippet(raw: str) -> str:
    return html.escape(raw or "").replace(HL_START, "<mark>").replace(HL_STOP, "</mark>")


async def hybrid_search(pool, embedder, query: str, *, mode: str = "hybrid", limit: int = 20,
                        filters: SearchFilters | None = None, pgvector_version: str = "0") -> list[dict]:
    query = (query or "").strip()
    if not query:
        return []
    filters = filters or SearchFilters()
    if mode not in ("hybrid", "keyword", "semantic"):
        mode = "hybrid"
    sql, params = build_query(mode, filters)
    params.update(q=query, k=CANDIDATES, rrf_k=RRF_K, limit=max(1, min(limit, 100)))
    if mode != "keyword":
        import asyncio

        params["emb"] = to_pgvector(await asyncio.to_thread(embedder.embed_query, query))
    async with pool.connection() as conn:
        async with conn.transaction():
            if filters.active:
                # Filters are applied after the HNSW scan, so widen the candidate pool.
                await conn.execute("SET LOCAL hnsw.ef_search = 400")
                if tuple(int(x) for x in pgvector_version.split(".")[:2]) >= (0, 8):
                    await conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(sql, params)
                rows = await cur.fetchall()
    for r in rows:
        r["snippet"] = render_snippet(r["snippet"])
        r["rrf_score"] = float(r["rrf_score"])
    return rows
