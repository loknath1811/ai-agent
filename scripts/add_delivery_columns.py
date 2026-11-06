"""One-time upgrade for an EXISTING database: adds the delivery columns to `tasks`.

    python -m scripts.add_delivery_columns

Safe to run more than once. (A brand-new database gets these columns automatically.)
"""
import asyncio

from app.database import async_engine

STEPS = [
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS delivery_status VARCHAR NOT NULL DEFAULT 'waiting'",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS delivery_attempts INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS delivery_error TEXT",
    "ALTER TABLE tasks ADD COLUMN IF NOT EXISTS delivery_next_at TIMESTAMP",
    "CREATE INDEX IF NOT EXISTS ix_tasks_delivery_status ON tasks (delivery_status)",
    # tasks that were already emailed before this upgrade
    "UPDATE tasks SET delivery_status = 'sent', delivery_attempts = 1 WHERE notified_at IS NOT NULL",
]


async def main() -> None:
    async with async_engine.begin() as conn:
        for sql in STEPS:
            await conn.exec_driver_sql(sql)
            print("ok:", sql[:80])
    await async_engine.dispose()
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
