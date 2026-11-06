"""Create all tables (only creates what is missing; never deletes data)."""
import asyncio

from app.database import async_engine
from app.models import Base


async def main() -> None:
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    await async_engine.dispose()
    print("Tables ready.")


asyncio.run(main())
