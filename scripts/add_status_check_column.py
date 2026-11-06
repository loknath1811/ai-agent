"""One-time upgrade for an EXISTING database: adds the status_check column to `tasks`.

    python -m scripts.add_status_check_column

Safe to run more than once. (A brand-new database gets the column automatically.)
"""
import asyncio

from app.database import async_engine

STEPS = ["ALTER TABLE tasks ADD COLUMN IF NOT EXISTS status_check VARCHAR"]


async def main() -> None:
    async with async_engine.begin() as conn:
        for sql in STEPS:
            await conn.exec_driver_sql(sql)
            print("ok:", sql)
    await async_engine.dispose()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
