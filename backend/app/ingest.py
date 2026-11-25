"""Index every event's content for the assistant.

    python -m app.ingest                 # all events, only changed cards are re-embedded
    python -m app.ingest --event <uuid>

The owner role is used only to list event ids; all writes go through app_rw under a `system`
request context, so RLS applies to the indexing itself.
"""

import argparse
import asyncio
import time
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.core.context import RequestContext, scoped_session
from app.core.db import dispose_engine
from app.core.embeddings import get_embedder
from app.modules.assistant.indexer import index_event


async def run(event_id: uuid.UUID | None = None) -> None:
    owner = create_async_engine(get_settings().migration_database_url)
    async with owner.connect() as conn:
        events = (
            await conn.execute(
                text(
                    "SELECT tenant_id, id, name FROM events "
                    "WHERE CAST(:e AS uuid) IS NULL OR id = CAST(:e AS uuid) ORDER BY name"
                ),
                {"e": event_id},
            )
        ).all()
    await owner.dispose()
    embedder = get_embedder()
    for tenant_id, eid, name in events:
        t0 = time.perf_counter()
        async with scoped_session(RequestContext.system(tenant_id, eid)) as db:
            stats = await index_event(db, embedder)
        print(
            f"{name}: {stats.cards} cards, {stats.embedded} embedded, "
            f"{stats.unchanged} unchanged, {stats.deleted} deleted "
            f"({time.perf_counter() - t0:.1f}s)"
        )
    await dispose_engine()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--event", type=uuid.UUID, default=None)
    asyncio.run(run(ap.parse_args().event))


if __name__ == "__main__":
    main()
