"""Retrieval, always executed in the caller's RLS-scoped session.

* hybrid_search: pgvector cosine top-N + Postgres full-text top-N, merged with reciprocal rank
  fusion. RLS on `embeddings` drops rows the caller may not see (visibility matrix), and the
  explicit tenant/event predicate is a second guard.
* lookups: exact structured answers for room / time / booth questions straight from SQL.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embeddings import EmbeddingService
from app.modules.assistant.indexer import vector_literal

RRF_K = 60


@dataclass
class Source:
    id: uuid.UUID
    entity_type: str
    entity_id: uuid.UUID
    title: str
    content: str
    visibility: str
    similarity: float
    keyword_hit: bool
    score: float


_HYBRID = text(
    """
WITH q AS (SELECT CAST(:qv AS vector) AS v, websearch_to_tsquery('english', :qt) AS ts),
vec AS (
  SELECT e.id, row_number() OVER (ORDER BY e.embedding <=> q.v) AS r
  FROM embeddings e, q
  WHERE e.tenant_id = app_tenant() AND e.event_id = app_event()
  ORDER BY e.embedding <=> q.v LIMIT :n
),
kw AS (
  SELECT e.id, row_number() OVER (ORDER BY ts_rank_cd(e.tsv, q.ts) DESC) AS r
  FROM embeddings e, q
  WHERE e.tenant_id = app_tenant() AND e.event_id = app_event() AND e.tsv @@ q.ts
  ORDER BY ts_rank_cd(e.tsv, q.ts) DESC LIMIT :n
)
SELECT e.id, e.entity_type, e.entity_id, e.title, e.content, e.visibility,
       1 - (e.embedding <=> q.v) AS similarity,
       kw.r IS NOT NULL AS keyword_hit,
       coalesce(1.0 / (:rrf + vec.r), 0) + coalesce(1.0 / (:rrf + kw.r), 0) AS score
FROM embeddings e
CROSS JOIN q
LEFT JOIN vec ON vec.id = e.id
LEFT JOIN kw ON kw.id = e.id
WHERE vec.id IS NOT NULL OR kw.id IS NOT NULL
ORDER BY score DESC
LIMIT :k
"""
)


async def hybrid_search(
    db: AsyncSession, embedder: EmbeddingService, query: str, k: int = 6, candidates: int = 20
) -> list[Source]:
    qv = await embedder.embed_query(query)
    rows = await db.execute(
        _HYBRID, {"qv": vector_literal(qv), "qt": query, "n": candidates, "k": k, "rrf": RRF_K}
    )
    return [Source(**r) for r in rows.mappings()]


# --- exact lookups ----------------------------------------------------------------------------


@dataclass
class Fact:
    entity_type: str
    entity_id: uuid.UUID
    title: str
    text: str
    score: float


def _when(starts: datetime, ends: datetime) -> str:
    return f"{starts:%a %b} {starts.day}, {starts:%H:%M}-{ends:%H:%M} UTC"


_MATCH = """greatest(word_similarity(lower(:t), lower({col})), similarity(lower(:t), lower({col})))
            + CASE WHEN to_tsvector('english', {col}) @@ plainto_tsquery('english', :t)
                   THEN 0.5 ELSE 0 END"""


async def lookup_sessions(db: AsyncSession, term: str, limit: int = 3) -> list[Fact]:
    rows = await db.execute(
        text(
            f"""SELECT s.id, s.title, s.kind, s.starts_at, s.ends_at, r.name AS room, r.floor,
                       {_MATCH.format(col="s.title")} AS score
                FROM sessions s LEFT JOIN rooms r ON r.id = s.room_id
                WHERE {_MATCH.format(col="s.title")} > 0.45
                ORDER BY score DESC, s.starts_at LIMIT :l"""
        ),
        {"t": term, "l": limit},
    )
    return [
        Fact(
            "session",
            r.id,
            r.title,
            f"{r.title} ({r.kind}) is in {r.room or 'a room to be announced'}"
            f"{f' ({r.floor})' if r.floor else ''} on {_when(r.starts_at, r.ends_at)}.",
            float(r.score),
        )
        for r in rows
    ]


async def lookup_exhibitors(db: AsyncSession, term: str, limit: int = 3) -> list[Fact]:
    rows = await db.execute(
        text(
            f"""SELECT id, name, booth, category,
                       CASE WHEN upper(booth) = upper(:t) THEN 2
                            ELSE {_MATCH.format(col="name")} END AS score
                FROM exhibitors
                WHERE upper(booth) = upper(:t) OR {_MATCH.format(col="name")} > 0.5
                ORDER BY score DESC LIMIT :l"""
        ),
        {"t": term, "l": limit},
    )
    return [
        Fact(
            "exhibitor",
            r.id,
            r.name,
            f"{r.name} ({r.category}) is at booth {r.booth}.",
            float(r.score),
        )
        for r in rows
    ]


async def lookup_speakers(db: AsyncSession, term: str, limit: int = 3) -> list[Fact]:
    rows = await db.execute(
        text(
            f"""SELECT sp.id, sp.full_name, sp.title, sp.company,
                       {_MATCH.format(col="sp.full_name")} AS score,
                       coalesce(array_agg(s.title || ' in ' || coalesce(r.name, 'TBA') || ', '
                                          || to_char(s.starts_at, 'Dy Mon DD HH24:MI') || ' UTC'
                                          ORDER BY s.starts_at)
                                FILTER (WHERE s.id IS NOT NULL), '{{}}') AS sessions
                FROM speakers sp
                LEFT JOIN session_speakers ss ON ss.speaker_id = sp.id
                LEFT JOIN sessions s ON s.id = ss.session_id
                LEFT JOIN rooms r ON r.id = s.room_id
                WHERE {_MATCH.format(col="sp.full_name")} > 0.5
                GROUP BY sp.id ORDER BY score DESC LIMIT :l"""
        ),
        {"t": term, "l": limit},
    )
    return [
        Fact(
            "speaker",
            r.id,
            r.full_name,
            f"{r.full_name} ({r.title}, {r.company}) speaks at: "
            f"{'; '.join(r.sessions) or 'no scheduled sessions'}.",
            float(r.score),
        )
        for r in rows
    ]


async def lookup_rooms(db: AsyncSession, term: str, limit: int = 3) -> list[Fact]:
    rows = await db.execute(
        text(
            f"""SELECT id, name, floor, capacity, {_MATCH.format(col="name")} AS score
                FROM rooms WHERE {_MATCH.format(col="name")} > 0.5
                ORDER BY score DESC LIMIT :l"""
        ),
        {"t": term, "l": limit},
    )
    return [
        Fact(
            "room",
            r.id,
            r.name,
            f"{r.name} is on {r.floor} and seats {r.capacity}.",
            float(r.score),
        )  # fmt: skip
        for r in rows
    ]


LOOKUPS = {
    "session": lookup_sessions,
    "exhibitor": lookup_exhibitors,
    "speaker": lookup_speakers,
    "room": lookup_rooms,
}
