import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.responses import FileResponse

from . import changes
from .config import get_settings
from .database import async_engine
from .log_config import configure_logging
from .models import Base
from .routes_agent import router as agent_router
from .routes_org import router as org_router
from .routes_reports import router as reports_router
from .routes_tasks import router as tasks_router
from .scheduler import shutdown_scheduler, start_scheduler
from .schema_upgrade import add_missing_columns
from .security import require_api_key

configure_logging()
log = logging.getLogger(__name__)
DASHBOARD_FILE = Path(__file__).resolve().parent.parent / "dashboard" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    if s.auto_create_tables:
        async with async_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await conn.run_sync(add_missing_columns)       # older database + newer code: add the new columns
    if s.enable_scheduler:
        start_scheduler()
    yield
    shutdown_scheduler()
    await async_engine.dispose()


app = FastAPI(title="Dhruva Work-Delegation Agent", lifespan=lifespan)

app.include_router(org_router)
app.include_router(tasks_router)
app.include_router(reports_router)
app.include_router(agent_router)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.get("/changes", dependencies=[Depends(require_api_key)])
async def get_changes() -> dict:
    """The dashboard's live check: a number that goes up whenever tasks or people change.
    Answered from memory (no database), so it is cheap to ask every couple of seconds."""
    return {"version": changes.current()}


@app.get("/dashboard", include_in_schema=False)
async def dashboard():
    """The page itself is static; the data it loads is protected by the API key."""
    return FileResponse(DASHBOARD_FILE)
