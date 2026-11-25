"""Turn event entities into text cards for retrieval.

One card per session / speaker / exhibitor / sponsor / FAQ, one for the event itself, and
documents split into overlapping chunks. Every card carries the visibility of its source row,
so RLS on `embeddings` hides it exactly like the source. Cards are built under a `system`
context (which sees everything in the event); readers only ever see what RLS lets through.
"""

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

ENTITY_TYPES = ("event", "session", "speaker", "exhibitor", "sponsor", "faq", "document")


@dataclass
class Card:
    entity_type: str
    entity_id: uuid.UUID
    title: str
    content: str
    visibility: str = "public"
    owner_exhibitor_id: uuid.UUID | None = None
    chunk_index: int = 0

    @property
    def key(self) -> tuple[str, uuid.UUID, int]:
        return self.entity_type, self.entity_id, self.chunk_index

    @property
    def content_hash(self) -> str:
        raw = "\x1f".join(
            [self.title, self.content, self.visibility, str(self.owner_exhibitor_id or "")]
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    @property
    def embed_text(self) -> str:
        return f"{self.title}\n{self.content}"


def fmt_when(starts: datetime, ends: datetime) -> str:
    return f"{starts:%A %B} {starts.day} {starts:%Y}, {starts:%H:%M}-{ends:%H:%M} UTC"


def chunk_text(content: str, max_chars: int = 700, overlap_sentences: int = 1) -> list[str]:
    """Sentence-aware chunks of roughly max_chars, overlapping by one sentence."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", content.strip()) if s.strip()]
    chunks: list[str] = []
    current: list[str] = []
    for sentence in sentences:
        if current and len(" ".join([*current, sentence])) > max_chars:
            chunks.append(" ".join(current))
            current = current[-overlap_sentences:] if overlap_sentences else []
        current.append(sentence)
    if current:
        chunks.append(" ".join(current))
    return chunks or [content]


def _id_filter(column: str) -> str:
    return f"(CAST(:id AS uuid) IS NULL OR {column} = CAST(:id AS uuid))"


async def _rows(db: AsyncSession, sql: str, entity_id: uuid.UUID | None):
    return (await db.execute(text(sql), {"id": entity_id})).mappings().all()


async def event_cards(db: AsyncSession, entity_id=None) -> list[Card]:
    rows = await _rows(
        db,
        f"""SELECT e.id, e.name, e.venue, e.starts_on, e.ends_on, e.kind,
                  (SELECT string_agg(name, ', ' ORDER BY name) FROM tracks) AS tracks
           FROM events e WHERE e.id = app_event() AND {_id_filter("e.id")}""",
        entity_id,
    )
    return [
        Card(
            "event",
            r["id"],
            r["name"],
            f"{r['name']} is a {r['kind']} event at {r['venue']}, running "
            f"{r['starts_on']:%B} {r['starts_on'].day} to {r['ends_on']:%B} {r['ends_on'].day} "
            f"{r['ends_on']:%Y}. Tracks: {r['tracks'] or 'none'}.",
        )
        for r in rows
    ]


async def session_cards(db: AsyncSession, entity_id=None) -> list[Card]:
    rows = await _rows(
        db,
        f"""SELECT s.id, s.title, s.kind, s.starts_at, s.ends_at, s.capacity, s.tags,
                  s.description, t.name AS track, r.name AS room, r.floor,
                  coalesce(array_agg(sp.full_name ORDER BY sp.full_name)
                           FILTER (WHERE sp.id IS NOT NULL), '{{}}') AS speakers
           FROM sessions s
           LEFT JOIN tracks t ON t.id = s.track_id
           LEFT JOIN rooms r ON r.id = s.room_id
           LEFT JOIN session_speakers ss ON ss.session_id = s.id
           LEFT JOIN speakers sp ON sp.id = ss.speaker_id
           WHERE {_id_filter("s.id")}
           GROUP BY s.id, t.name, r.name, r.floor""",
        entity_id,
    )
    cards = []
    for r in rows:
        where = f"{r['room']} ({r['floor']})" if r["room"] else "room to be announced"
        cards.append(
            Card(
                "session",
                r["id"],
                r["title"],
                f"Session: {r['title']}\nType: {r['kind']}\nTrack: {r['track']}\n"
                f"When: {fmt_when(r['starts_at'], r['ends_at'])}\nRoom: {where}\n"
                f"Capacity: {r['capacity']} seats\n"
                f"Speakers: {', '.join(r['speakers']) or 'to be announced'}\n"
                f"Topics: {', '.join(r['tags'])}\n{r['description'] or ''}".strip(),
            )
        )
    return cards


async def speaker_cards(db: AsyncSession, entity_id=None) -> list[Card]:
    rows = await _rows(
        db,
        f"""SELECT sp.id, sp.full_name, sp.title, sp.company, sp.bio, sp.topics,
                  coalesce(array_agg(s.title || ' (' || coalesce(r.name, 'TBA') || ', '
                                     || to_char(s.starts_at, 'Dy Mon DD HH24:MI') || ' UTC)'
                                     ORDER BY s.starts_at)
                           FILTER (WHERE s.id IS NOT NULL), '{{}}') AS sessions
           FROM speakers sp
           LEFT JOIN session_speakers ss ON ss.speaker_id = sp.id
           LEFT JOIN sessions s ON s.id = ss.session_id
           LEFT JOIN rooms r ON r.id = s.room_id
           WHERE {_id_filter("sp.id")}
           GROUP BY sp.id""",
        entity_id,
    )
    return [
        Card(
            "speaker",
            r["id"],
            r["full_name"],
            f"Speaker: {r['full_name']}, {r['title']} at {r['company']}.\n{r['bio'] or ''}\n"
            f"Topics: {', '.join(r['topics'])}\n"
            f"Speaking at: {'; '.join(r['sessions']) or 'no sessions yet'}",
        )
        for r in rows
    ]


async def exhibitor_cards(db: AsyncSession, entity_id=None) -> list[Card]:
    rows = await _rows(
        db,
        f"""SELECT id, name, booth, category, description, tags, website FROM exhibitors
           WHERE {_id_filter("id")}""",
        entity_id,
    )
    return [
        Card(
            "exhibitor",
            r["id"],
            r["name"],
            f"Exhibitor: {r['name']}\nBooth: {r['booth']}\nCategory: {r['category']}\n"
            f"Focus: {', '.join(r['tags'])}\n{r['description'] or ''}\nWebsite: {r['website']}",
        )
        for r in rows
    ]


async def sponsor_cards(db: AsyncSession, entity_id=None) -> list[Card]:
    rows = await _rows(
        db,
        f"""SELECT id, name, tier, description, website FROM sponsors WHERE {_id_filter("id")}""",
        entity_id,
    )
    return [
        Card(
            "sponsor",
            r["id"],
            f"{r['name']} ({r['tier']} sponsor)",
            f"{r['name']} is a {r['tier']} sponsor. {r['description'] or ''}",
        )
        for r in rows
    ]


async def faq_cards(db: AsyncSession, entity_id=None) -> list[Card]:
    rows = await _rows(
        db,
        f"""SELECT id, question, answer, category, visibility, owner_exhibitor_id FROM faqs
           WHERE {_id_filter("id")}""",
        entity_id,
    )
    return [
        Card(
            "faq",
            r["id"],
            r["question"],
            f"Q: {r['question']}\nA: {r['answer']}",
            r["visibility"],
            r["owner_exhibitor_id"],
        )
        for r in rows
    ]


async def document_cards(db: AsyncSession, entity_id=None) -> list[Card]:
    rows = await _rows(
        db,
        f"""SELECT id, title, content, visibility, owner_exhibitor_id FROM documents
           WHERE {_id_filter("id")}""",
        entity_id,
    )
    cards = []
    for r in rows:
        for i, chunk in enumerate(chunk_text(r["content"])):
            cards.append(
                Card(
                    "document",
                    r["id"],
                    r["title"],
                    chunk,
                    r["visibility"],
                    r["owner_exhibitor_id"],
                    chunk_index=i,
                )  # fmt: skip
            )
    return cards


BUILDERS = {
    "event": event_cards,
    "session": session_cards,
    "speaker": speaker_cards,
    "exhibitor": exhibitor_cards,
    "sponsor": sponsor_cards,
    "faq": faq_cards,
    "document": document_cards,
}


async def build_cards(
    db: AsyncSession, entity_type: str | None = None, entity_id: uuid.UUID | None = None
) -> list[Card]:
    types = [entity_type] if entity_type else list(BUILDERS)
    cards: list[Card] = []
    for t in types:
        cards += await BUILDERS[t](db, entity_id)
    return cards
