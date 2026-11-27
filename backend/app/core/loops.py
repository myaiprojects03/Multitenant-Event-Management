"""Event-loop awareness for serverless runtimes, which may give each request a fresh loop.

Network clients (asyncpg pools, redis, httpx) are bound to the loop they connected on; reusing
one from a finished loop fails. In serverless mode the cached clients are rebuilt per loop.
"""

import asyncio


def current_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None
