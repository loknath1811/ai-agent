"""Idempotency: asking for the same assignment twice must not create two tasks or send two emails.

Each test is one real way a duplicate happens:
  * the AI calls the assign tool twice          -> test_same_request_twice...
  * a double click                               -> same code path
  * the server dies before it logs an email      -> test_reprocessing_same_email...
  * two requests at the same instant             -> test_simultaneous_...  and  test_database_rule_...
"""
import asyncio
from datetime import timedelta

import httpx
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import select

from app import directory
from app.database import async_session
from app.llm_tasks import AssignmentItem
from app.main import app
from app.models import OutboundEmail, Task
from app.services import inbox
from app.services import tasks as task_service
from app.services.assign import assign_from_items

from .conftest import make_email


def item(**kw):
    base = dict(title="NCR review", assignee="Ravi Kumar", due_date="2030-01-10")
    base.update(kw)
    return AssignmentItem(**base)


async def leader(s, email="ceo@example.com"):
    return await directory.get_by_email(s, email)


async def task_count():
    async with async_session() as s:
        return len((await s.execute(select(Task))).scalars().all())


# ---------------------------------------------------------------- the main case
async def test_same_request_twice_creates_one_task_and_one_email(org, sent):
    async with async_session() as s:
        first = await assign_from_items(s, await leader(s), [item()])
        second = await assign_from_items(s, await leader(s), [item()])

    assert len(first.created) == 1
    assert second.created == []                                  # nothing new
    assert [t.id for t in second.duplicates] == [first.created[0].id]   # it points at the original
    assert await task_count() == 1
    assert len(sent) == 1                                        # Ravi was emailed once, not twice
    assert "already assigned" in second.summary()                # the leader is TOLD, never silently ignored


async def test_small_differences_in_title_still_count_as_the_same(org, sent):
    async with async_session() as s:
        await assign_from_items(s, await leader(s), [item(title="NCR review")])
        again = await assign_from_items(s, await leader(s), [item(title="  ncr   REVIEW ")])
    assert again.created == [] and await task_count() == 1


# ---------------------------------------------------------------- what must NOT be blocked
async def test_different_deadline_assignee_title_or_assigner_is_a_new_task(org, sent):
    async with async_session() as s:
        await assign_from_items(s, await leader(s), [item()])
        for other in (item(due_date="2030-02-01"),            # different deadline
                      item(assignee="Meena"),                  # different person
                      item(title="Safety audit")):             # different work
            res = await assign_from_items(s, await leader(s), [other])
            assert len(res.created) == 1, other
        res = await assign_from_items(s, await leader(s, "anita@example.com"), [item()])   # different assigner
        assert len(res.created) == 1
    assert await task_count() == 5


async def test_same_request_after_the_window_is_allowed(org, sent, monkeypatch):
    """A weekly 'Send report' must still work: the block only lasts DUPLICATE_WINDOW_MINUTES."""
    async with async_session() as s:
        await assign_from_items(s, await leader(s), [item()])
    real_now = task_service.utcnow()
    monkeypatch.setattr(task_service, "utcnow", lambda: real_now + timedelta(minutes=11))
    async with async_session() as s:
        res = await assign_from_items(s, await leader(s), [item()])
    assert len(res.created) == 1 and await task_count() == 2


async def test_team_fan_out_duplicates_are_caught_per_person(org, sent):
    async with async_session() as s:
        a = await assign_from_items(s, await leader(s), [item(assignee="QA team", fan_out=True)])
        b = await assign_from_items(s, await leader(s), [item(assignee="QA team", fan_out=True)])
    assert len(a.created) == 3 and b.created == [] and len(b.duplicates) == 3
    assert await task_count() == 3 and len(sent) == 3


# ---------------------------------------------------------------- the race (two at the same instant)
async def test_database_rule_catches_a_race_the_check_missed(org, sent, monkeypatch):
    """Simulate two requests that BOTH looked first and found nothing (the gap in 'check, then insert').
    The second insert must be refused by the database and handled as 'already exists'."""
    async with async_session() as s:
        first = await assign_from_items(s, await leader(s), [item()])
    original = first.created[0].id

    async def blind_check(*a, **k):          # the check "sees nothing", as it would in a real race
        return None
    monkeypatch.setattr(task_service, "find_recent_duplicate", blind_check)

    async with async_session() as s:
        second = await assign_from_items(s, await leader(s), [item()])

    assert second.created == [] and [t.id for t in second.duplicates] == [original]
    assert await task_count() == 1 and len(sent) == 1


async def test_simultaneous_identical_requests_make_one_task(org, sent):
    async def one():
        async with async_session() as s:
            return await assign_from_items(s, await leader(s), [item()])
    results = await asyncio.gather(one(), one(), one())
    assert sum(len(r.created) for r in results) == 1
    assert await task_count() == 1 and len(sent) == 1


# ---------------------------------------------------------------- crash between "tasks saved" and "email logged"
async def test_reprocessing_same_email_creates_no_second_task(org, sent, llm):
    """The server died after saving the tasks but before writing the 'email handled' log row,
    so on restart the very same email is read again."""
    llm.assign_json = {"is_assignment": True,
                       "items": [{"title": "Send drawings", "assignee": "Meena", "due_date": "2030-03-01"}]}
    em = make_email("ceo@example.com", subject="Please assign", body="Ask Meena to send the drawings")
    async with async_session() as s:
        await inbox.handle_email(s, em)
    async with async_session() as s:
        await inbox.handle_email(s, em)          # same email again, as after a restart
    assert await task_count() == 1
    async with async_session() as s:
        assignment_mails = (await s.execute(
            select(OutboundEmail).where(OutboundEmail.kind == "assignment"))).scalars().all()
    assert len(assignment_mails) == 1


# ---------------------------------------------------------------- the REST API
@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_api_second_identical_post_returns_the_existing_task(org, sent, client):
    body = {"title": "NCR review", "assignee": "ravi.kumar@example.com",
            "assigned_by_email": "ceo@example.com", "due_date": "2030-01-10"}
    r1 = await client.post("/tasks", json=body)
    r2 = await client.post("/tasks", json=body)
    assert r1.status_code == 201
    assert r2.status_code == 200                                  # not an error: same answer as the first time
    assert r2.json()[0]["id"] == r1.json()[0]["id"]
    assert await task_count() == 1
