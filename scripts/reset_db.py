"""DANGER: deletes ALL tables (including your old `tasks`) and rebuilds them empty.

Use once, to move from the old prototype schema to this one:
    python -m scripts.reset_db --yes
"""
import asyncio
import sys

from sqlalchemy import text

from app.database import async_engine
from app.models import Base


async def main() -> None:
    if "--yes" not in sys.argv:
        sys.exit("This deletes every table and all data. Re-run with --yes to confirm.")
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        if conn.dialect.name == "postgresql":
            await conn.execute(text("DROP TABLE IF EXISTS tasks CASCADE"))   # old prototype table
            await conn.execute(text("DROP TYPE IF EXISTS taskstatus"))
            await conn.execute(text("DROP TABLE IF EXISTS alembic_version"))  # old half-set-up Alembic
        await conn.run_sync(Base.metadata.create_all)
    await async_engine.dispose()
    print("Database reset. Tables are empty.")


asyncio.run(main())
