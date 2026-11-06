from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from . import changes, metrics
from .config import get_settings

_settings = get_settings()

if not _settings.database_url:
    raise RuntimeError("DATABASE_URL missing from .env")


def build_async_url(raw: str):
    """Turn a normal DATABASE_URL into one SQLAlchemy can use asynchronously.

    - postgres:// or postgresql:// -> postgresql+asyncpg://
    - sslmode=... (libpq style)    -> ssl=... (asyncpg style)
    - channel_binding=...          -> dropped (asyncpg does not accept it)
    - sqlite:// stays sqlite (used by the automated tests)
    """
    url = make_url(raw)
    if url.drivername.startswith("postgres"):
        query = dict(url.query)
        sslmode = query.pop("sslmode", None)
        query.pop("channel_binding", None)
        if sslmode and "ssl" not in query:
            query["ssl"] = sslmode
        url = url.set(drivername="postgresql+asyncpg", query=query)
    elif url.drivername == "sqlite":
        url = url.set(drivername="sqlite+aiosqlite")
    return url


async_engine: AsyncEngine = create_async_engine(
    build_async_url(_settings.database_url), echo=False, pool_pre_ping=True
)

metrics.install_db_hooks(async_engine)
changes.install()

async_session = async_sessionmaker(async_engine, expire_on_commit=False)


async def get_session():
    """FastAPI dependency: one session per request."""
    async with async_session() as session:
        yield session
