"""Measure raw database speed, with no AI and no email involved.

    python -m scripts.db_ping

Shows: how long a fresh connection takes, how long ONE tiny query takes (a pure round trip),
and how long the app's normal 'open a session + run a query' takes.
"""
import asyncio
import statistics
import time

from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.config import get_settings
from app.database import async_engine, async_session


def ms(x: float) -> str:
    return f"{x * 1000:7.0f} ms"


async def main() -> None:
    url = make_url(get_settings().database_url)
    host = url.host or "(local file)"
    print(f"database host: {host}")
    print(f"  uses Neon pooler endpoint: {'-pooler' in host}")
    print()

    # 1. a brand-new connection (TCP + SSL + login)
    t = time.perf_counter()
    conn = await async_engine.connect()
    print(f"1. new connection (TCP+SSL+login) : {ms(time.perf_counter() - t)}")

    # 2. pure round trip: same connection, tiny query, repeated
    await conn.execute(text("SELECT 1"))  # warm up
    times = []
    for _ in range(10):
        t = time.perf_counter()
        await conn.execute(text("SELECT 1"))
        times.append(time.perf_counter() - t)
    await conn.close()
    print(f"2. one tiny query, 10 tries       : avg {ms(statistics.mean(times))}"
          f"   min {ms(min(times))}   max {ms(max(times))}")
    print("   (this is your network round trip to the database)")

    # 3. how the app really does it: open a session, run one query, close
    times = []
    for _ in range(5):
        t = time.perf_counter()
        async with async_session() as s:
            await s.execute(text("SELECT 1"))
        times.append(time.perf_counter() - t)
    print(f"3. app-style session + 1 query    : avg {ms(statistics.mean(times))}"
          f"   (first {ms(times[0])}, then {ms(statistics.mean(times[1:]))})")

    # 4. after being idle: does the database/connection wake up slowly?
    print("\nwaiting 30 seconds idle, then testing again ...")
    await asyncio.sleep(30)
    t = time.perf_counter()
    async with async_session() as s:
        await s.execute(text("SELECT 1"))
    print(f"4. first query after 30s idle     : {ms(time.perf_counter() - t)}")
    await async_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())