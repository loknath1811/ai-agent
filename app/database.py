import os
from urllib.parse import urlparse

from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    create_async_engine,
    async_sessionmaker,
)

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL missing from .env")


# Convert PostgreSQL URL for asyncpg
parsed = urlparse(DATABASE_URL)

ASYNC_DATABASE_URL = (
    f"postgresql+asyncpg://"
    f"{parsed.username}:{parsed.password}"
    f"@{parsed.hostname}"
)

if parsed.port:
    ASYNC_DATABASE_URL += f":{parsed.port}"

ASYNC_DATABASE_URL += f"{parsed.path}"


async_engine: AsyncEngine = create_async_engine(
    ASYNC_DATABASE_URL,
    echo=False,
)

async_session = async_sessionmaker(
    async_engine,
    expire_on_commit=False,
)