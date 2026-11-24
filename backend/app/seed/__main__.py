"""Reset and load the synthetic demo data.

    python -m app.seed               # anchor = today (UTC): main events ran the last 2 days
    python -m app.seed --anchor 2026-11-24 --seed 42

Runs as the owner role (bypasses RLS) because it writes every tenant.
"""

import argparse
import asyncio
import time
from collections import Counter
from datetime import date

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.models import Base
from app.seed.generator import generate

CHUNK = 1000


async def load(rows: dict[str, list[dict]]) -> None:
    engine = create_async_engine(get_settings().migration_database_url)
    tables = Base.metadata.sorted_tables
    async with engine.begin() as conn:
        names = ", ".join(t.name for t in tables)
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        for table in tables:
            data = rows.get(table.name, [])
            # executemany needs identical keys per batch
            by_keys: dict[tuple, list[dict]] = {}
            for r in data:
                by_keys.setdefault(tuple(sorted(r)), []).append(r)
            for group in by_keys.values():
                for i in range(0, len(group), CHUNK):
                    await conn.execute(table.insert(), group[i : i + CHUNK])
    await engine.dispose()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--anchor", type=date.fromisoformat, default=None)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    t0 = time.perf_counter()
    rows = generate(args.anchor, args.seed)
    asyncio.run(load(rows))
    counts = Counter({k: len(v) for k, v in rows.items()})
    print(f"seeded in {time.perf_counter() - t0:.1f}s")
    for name, n in sorted(counts.items()):
        print(f"  {name:<24} {n:>7}")


if __name__ == "__main__":
    main()
