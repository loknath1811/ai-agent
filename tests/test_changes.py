"""The live-dashboard counter: goes up after real changes, stays put otherwise."""
import dataclasses

import httpx
from httpx import ASGITransport

from app import changes, directory
from app.config import get_settings
from app.database import async_session
from app.llm_tasks import AssignmentItem
from app.main import app
from app.models import Employee, TaskEvent
from app.services import delivery, tasks as task_service
from app.services.assign import assign_from_items


async def test_counter_goes_up_after_a_task_is_created(org, sent):
    before = changes.current()
    async with async_session() as s:
        ceo = await directory.get_by_email(s, "ceo@example.com")
        await assign_from_items(s, ceo, [AssignmentItem(title="NCR review", assignee="Meena", due_date="2030-01-10")])
    assert changes.current() > before


async def test_counter_goes_up_when_an_email_is_delivered(org, sent, monkeypatch):
    s_on = dataclasses.replace(get_settings(), async_delivery=True)     # email waits for the background sender
    monkeypatch.setattr(task_service, "get_settings", lambda: s_on)
    async with async_session() as s:
        ceo = await directory.get_by_email(s, "ceo@example.com")
        await assign_from_items(s, ceo, [AssignmentItem(title="NCR review", assignee="Meena", due_date="2030-01-10")])
    before = changes.current()
    async with async_session() as s:
        await delivery.deliver_pending(s)
    assert changes.current() > before


async def test_counter_ignores_reads_and_idle_checks(org, sent):
    before = changes.current()
    async with async_session() as s:
        await directory.get_by_email(s, "ceo@example.com")        # a plain read
    async with async_session() as s:
        await delivery.deliver_pending(s)                          # nothing is waiting
    assert changes.current() == before


async def test_counter_ignores_history_only_writes_and_rollbacks(org, sent):
    before = changes.current()
    async with async_session() as s:
        s.add(TaskEvent(task_id=1, event_type="note"))             # an event row alone is not a visible change
        try:
            await s.commit()
        except Exception:
            await s.rollback()
    async with async_session() as s:
        e = await s.get(Employee, 1)
        e.full_name = "Changed then undone"
        await s.flush()
        await s.rollback()                                          # never committed
    assert changes.current() == before


async def test_changes_endpoint_returns_the_number(org):
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/changes")
    assert r.status_code == 200 and r.json() == {"version": changes.current()}
