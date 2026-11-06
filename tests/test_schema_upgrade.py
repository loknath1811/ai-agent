"""An older database (missing newer columns) is upgraded in place, without losing data."""
from sqlalchemy import inspect, select, text

from app.database import async_engine, async_session
from app.models import Employee, Task
from app.schema_upgrade import add_missing_columns

NEW_COLUMNS = ["status_check", "delivery_status", "delivery_attempts", "delivery_error", "delivery_next_at"]


async def _columns():
    async with async_engine.connect() as conn:
        return await conn.run_sync(lambda c: {col["name"] for col in inspect(c).get_columns("tasks")})


async def _make_old_database(org):
    """Take today's schema and remove the recently added columns, like a database made before the updates."""
    async with async_session() as s:
        ceo = (await s.execute(select(Employee).where(Employee.email == "ceo@example.com"))).scalar_one()
        meena = (await s.execute(select(Employee).where(Employee.email == "meena@example.com"))).scalar_one()
        s.add(Task(title="Old task", assignee_id=meena.id, assigned_by_id=ceo.id))
        await s.commit()
    async with async_engine.begin() as conn:
        await conn.execute(text("DROP INDEX IF EXISTS ix_tasks_delivery_status"))
        for c in NEW_COLUMNS:
            await conn.execute(text(f"ALTER TABLE tasks DROP COLUMN {c}"))
    assert not set(NEW_COLUMNS) & await _columns()


async def test_missing_columns_are_added_and_old_rows_survive(org):
    await _make_old_database(org)
    async with async_engine.begin() as conn:
        added = await conn.run_sync(add_missing_columns)
    assert sorted(added) == sorted(f"tasks.{c}" for c in NEW_COLUMNS)
    assert set(NEW_COLUMNS) <= await _columns()
    async with async_session() as s:                                  # the old row is intact and readable by the new code
        t = (await s.execute(select(Task).where(Task.title == "Old task"))).scalars().unique().one()
        assert t.status_check is None and t.delivery_status == "waiting" and t.delivery_attempts == 0


async def test_running_it_again_changes_nothing(org):
    await _make_old_database(org)
    async with async_engine.begin() as conn:
        await conn.run_sync(add_missing_columns)
    async with async_engine.begin() as conn:
        assert await conn.run_sync(add_missing_columns) == []


async def test_an_up_to_date_database_is_left_alone(org):
    async with async_engine.begin() as conn:
        assert await conn.run_sync(add_missing_columns) == []


async def test_the_app_starts_on_an_old_database(org):
    """The real startup path: the app itself must repair the schema before serving anything."""
    import httpx
    from httpx import ASGITransport
    from app.main import app, lifespan
    await _make_old_database(org)
    async with lifespan(app):
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/reports/company")
    assert r.status_code == 200
    assert set(NEW_COLUMNS) <= await _columns()
