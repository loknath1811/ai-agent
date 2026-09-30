import os
import asyncio
import asyncpg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")


async def main():
    conn = await asyncpg.connect(DATABASE_URL)

    await conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL
        )
    """)

    await conn.execute(
        "INSERT INTO tasks (title) VALUES ($1)",
        "Hello from AI Agent"
    )

    print("Database connection successful!")
    print("Task inserted successfully!")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(main())