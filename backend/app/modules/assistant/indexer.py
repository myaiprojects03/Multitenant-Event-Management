"""Index cards into `embeddings`, re-embedding only what changed (content hash).

Runs inside a `system`-scoped session, so every row written carries the event's tenant_id and
event_id and RLS still guards the write.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embeddings import EmbeddingService
from app.modules.assistant.cards import Card, build_cards

# entity changes that also change other cards (a speaker card lists that speaker's sessions)
_DEPENDENTS = {
    "session": """SELECT 'speaker' AS kind, speaker_id AS ref FROM session_speakers
                  WHERE session_id = :id""",
}


@dataclass
class IndexStats:
    cards: int = 0
    embedded: int = 0
    unchanged: int = 0
    deleted: int = 0

    def __iadd__(self, other: "IndexStats") -> "IndexStats":
        self.cards += other.cards
        self.embedded += other.embedded
        self.unchanged += other.unchanged
        self.deleted += other.deleted
        return self


def vector_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in v) + "]"


_UPSERT = text(
    """INSERT INTO embeddings (tenant_id, event_id, entity_type, entity_id, chunk_index, title,
                              content, content_hash, visibility, owner_exhibitor_id, embedding,
                              updated_at)
       VALUES (app_tenant(), app_event(), :entity_type, :entity_id, :chunk_index, :title,
               :content, :content_hash, :visibility, :owner, CAST(:embedding AS vector), now())
       ON CONFLICT (event_id, entity_type, entity_id, chunk_index) DO UPDATE SET
         title = EXCLUDED.title, content = EXCLUDED.content,
         content_hash = EXCLUDED.content_hash, visibility = EXCLUDED.visibility,
         owner_exhibitor_id = EXCLUDED.owner_exhibitor_id, embedding = EXCLUDED.embedding,
         updated_at = now()"""
)


async def _existing(db: AsyncSession, entity_type, entity_id) -> dict[tuple, str]:
    rows = await db.execute(
        text(
            """SELECT entity_type, entity_id, chunk_index, content_hash FROM embeddings
               WHERE (CAST(:t AS text) IS NULL OR entity_type = CAST(:t AS text))
                 AND (CAST(:id AS uuid) IS NULL OR entity_id = CAST(:id AS uuid))"""
        ),
        {"t": entity_type, "id": entity_id},
    )
    return {(r.entity_type, r.entity_id, r.chunk_index): r.content_hash for r in rows}


async def index_cards(
    db: AsyncSession,
    embedder: EmbeddingService,
    cards: list[Card],
    *,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
) -> IndexStats:
    """Sync the embeddings for the given scope (whole event, one type, or one entity)."""
    existing = await _existing(db, entity_type, entity_id)
    changed = [c for c in cards if existing.get(c.key) != c.content_hash]
    stats = IndexStats(cards=len(cards), embedded=len(changed))
    stats.unchanged = len(cards) - len(changed)
    if changed:
        vectors = await embedder.embed_passages([c.embed_text for c in changed])
        await db.execute(
            _UPSERT,
            [
                {
                    "entity_type": c.entity_type,
                    "entity_id": c.entity_id,
                    "chunk_index": c.chunk_index,
                    "title": c.title,
                    "content": c.content,
                    "content_hash": c.content_hash,
                    "visibility": c.visibility,
                    "owner": c.owner_exhibitor_id,
                    "embedding": vector_literal(v),
                }
                for c, v in zip(changed, vectors, strict=True)
            ],
        )
    stale = set(existing) - {c.key for c in cards}
    for t, eid, idx in stale:
        await db.execute(
            text(
                "DELETE FROM embeddings WHERE entity_type = :t AND entity_id = :id "
                "AND chunk_index = :i"
            ),
            {"t": t, "id": eid, "i": idx},
        )
    stats.deleted = len(stale)
    return stats


async def index_event(db: AsyncSession, embedder: EmbeddingService) -> IndexStats:
    return await index_cards(db, embedder, await build_cards(db))


async def reindex_entity(
    db: AsyncSession, embedder: EmbeddingService, entity_type: str, entity_id: uuid.UUID
) -> IndexStats:
    """Re-index one entity (and cards that embed it). Deleted entities lose their cards."""
    stats = IndexStats()
    targets = [(entity_type, entity_id)]
    if entity_type in _DEPENDENTS:
        rows = await db.execute(text(_DEPENDENTS[entity_type]), {"id": entity_id})
        targets += [(r.kind, r.ref) for r in rows]
    for t, eid in targets:
        cards = await build_cards(db, t, eid)
        stats += await index_cards(db, embedder, cards, entity_type=t, entity_id=eid)
    return stats
