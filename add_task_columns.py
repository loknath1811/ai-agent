import os
import asyncio
import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")


async def main():
    conn = await asyncpg.connect(DATABASE_URL)

    await conn.execute("""
        ALTER TABLE tasks
        ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'pending',
        ADD COLUMN IF NOT EXISTS created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    """)

    print("Task table updated successfully!")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())