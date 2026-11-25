"""Build matchmaking vectors (profile_vectors) for attendees, sessions, speakers, exhibitors.

Attendee text uses role, seniority, industry, persona, interests, needs, offers and goals,
deliberately not names or companies. Hash-based: only changed texts are re-embedded.
Runs under a `system` context.
"""

import hashlib
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embeddings import EmbeddingService
from app.modules.assistant.indexer import vector_literal


@dataclass
class VectorStats:
    total: int = 0
    embedded: int = 0


SOURCES = {
    "attendee": """SELECT id, concat_ws('. ',
                      job_title || ' (' || coalesce(seniority, '') || ') in '
                          || coalesce(industry, ''),
                      'Persona: ' || coalesce(persona_type, ''),
                      'Interests: ' || array_to_string(interests, ', '),
                      'Looking for: ' || array_to_string(looking_for, ', '),
                      'Offering: ' || array_to_string(offering, ', '),
                      goals) AS body
                   FROM attendee_profiles""",
    "session": """SELECT s.id, concat_ws('. ', s.title, 'Track: ' || t.name,
                      'Topics: ' || array_to_string(s.tags, ', '), s.description) AS body
                  FROM sessions s LEFT JOIN tracks t ON t.id = s.track_id""",
    "speaker": """SELECT id, concat_ws('. ', title, 'Topics: ' || array_to_string(topics, ', '),
                      bio) AS body FROM speakers""",
    "exhibitor": """SELECT id, concat_ws('. ', 'Category: ' || category,
                        'Focus: ' || array_to_string(tags, ', '), description) AS body
                    FROM exhibitors""",
}

_UPSERT = text(
    """INSERT INTO profile_vectors (tenant_id, event_id, entity_type, entity_id, content_hash,
                                   embedding, updated_at)
       VALUES (app_tenant(), app_event(), :t, :id, :h, CAST(:v AS vector), now())
       ON CONFLICT (event_id, entity_type, entity_id) DO UPDATE SET
         content_hash = EXCLUDED.content_hash, embedding = EXCLUDED.embedding, updated_at = now()"""
)


async def build_profile_vectors(db: AsyncSession, embedder: EmbeddingService) -> VectorStats:
    stats = VectorStats()
    for entity_type, sql in SOURCES.items():
        rows = (await db.execute(text(sql))).all()
        existing = dict(
            (
                await db.execute(
                    text(
                        "SELECT entity_id, content_hash FROM profile_vectors WHERE entity_type = :t"
                    ),
                    {"t": entity_type},
                )
            ).all()
        )
        todo = []
        for entity_id, body in rows:
            h = hashlib.sha256(body.encode()).hexdigest()
            if existing.get(entity_id) != h:
                todo.append((entity_id, body, h))
        stats.total += len(rows)
        if not todo:
            continue
        vectors = await embedder.embed_passages([b for _, b, _ in todo])
        await db.execute(
            _UPSERT,
            [
                {"t": entity_type, "id": eid, "h": h, "v": vector_literal(v)}
                for (eid, _, h), v in zip(todo, vectors, strict=True)
            ],
        )
        stats.embedded += len(todo)
    return stats
