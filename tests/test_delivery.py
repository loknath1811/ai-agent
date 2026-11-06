"""Background delivery of assignment emails: waiting -> sent / failed, retries, no double sending."""
import asyncio
import dataclasses
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import directory, email_client
from app.config import get_settings
from app.database import async_session
from app.llm_tasks import AssignmentItem
from app.models import Employee, Task, TaskEvent
from app.services import delivery
from app.services import tasks as task_service
from app.services.assign import assign_from_items
from app.timeutil import utcnow


@pytest.fixture(autouse=True)
def background_mode(monkeypatch):
    """These tests run with ASYNC_DELIVERY on (the rest of the suite runs with it off)."""
    s = dataclasses.replace(get_settings(), async_delivery=True)
    monkeypatch.setattr(task_service, "get_settings", lambda: s)


async def _assign_one():
    async with async_session() as s:
        ceo = await directory.get_by_email(s, "ceo@example.com")
        res = await assign_from_items(s, ceo, [AssignmentItem(title="NCR review", assignee="Meena", due_date="2030-01-10")])
        return res


async def _task(tid):
    async with async_session() as s:
        return await s.get(Task, tid)


async def test_assigning_does_not_send_but_marks_waiting(org, sent):
    res = await _assign_one()
    t = res.created[0]
    assert t.delivery_status == "waiting" and t.notified_at is None
    assert sent == []                                    # nothing was sent while the caller waited
    assert "being sent" in res.summary()                 # and the reply says so honestly


async def test_sender_delivers_and_marks_sent(org, sent):
    tid = (await _assign_one()).created[0].id
    async with async_session() as s:
        assert await delivery.deliver_pending(s) == 1
    assert len(sent) == 1 and sent[0]["to"] == "meena@example.com"
    t = await _task(tid)
    assert t.delivery_status == "sent" and t.notified_at is not None and t.delivery_attempts == 1
    async with async_session() as s:
        kinds = [e.event_type for e in (await s.execute(select(TaskEvent).order_by(TaskEvent.id))).scalars()]
    assert kinds == ["created", "delivered"]


async def test_already_sent_task_is_not_sent_again(org, sent):
    await _assign_one()
    async with async_session() as s:
        await delivery.deliver_pending(s)
    async with async_session() as s:
        assert await delivery.deliver_pending(s) == 0
    assert len(sent) == 1


async def test_two_senders_at_once_send_only_one_email(org, sent):
    await _assign_one()

    async def one_pass():
        async with async_session() as s:
            return await delivery.deliver_pending(s)
    results = await asyncio.gather(one_pass(), one_pass())
    assert len(sent) == 1 and sum(results) == 1


async def test_failure_retries_on_schedule_then_gives_up(org, monkeypatch):
    calls = {"n": 0}

    async def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("smtp down")
    monkeypatch.setattr(email_client, "send_email", boom)
    tid = (await _assign_one()).created[0].id
    t0 = utcnow()

    async def pass_at(minutes):
        async with async_session() as s:
            return await delivery.deliver_pending(s, now=t0 + timedelta(minutes=minutes))

    await pass_at(0)                                   # attempt 1 fails -> retry in 1 min
    t = await _task(tid)
    assert (t.delivery_status, t.delivery_attempts) == ("waiting", 1)
    assert "smtp down" in t.delivery_error
    await pass_at(0.5)                                 # too early: nothing happens
    assert calls["n"] == 1
    await pass_at(2)                                   # attempt 2 -> next in 5 min
    await pass_at(8)                                   # attempt 3 -> next in 15 min
    await pass_at(30)                                  # attempt 4 -> give up
    t = await _task(tid)
    assert (t.delivery_status, t.delivery_attempts) == ("failed", 4)
    await pass_at(120)                                 # a failed task is never retried again
    assert calls["n"] == 4
    async with async_session() as s:
        kinds = [e.event_type for e in (await s.execute(select(TaskEvent))).scalars()]
    assert kinds.count("delivery_failed") == 4


async def test_retry_succeeds_after_a_failure(org, monkeypatch):
    state = {"fail": True}

    async def flaky(to, subject, body, cc=None, in_reply_to=None, task_id=None, html=None):
        if state["fail"]:
            raise RuntimeError("smtp down")
        return "<ok@test>"
    monkeypatch.setattr(email_client, "send_email", flaky)
    tid = (await _assign_one()).created[0].id
    async with async_session() as s:
        await delivery.deliver_pending(s)
    state["fail"] = False
    async with async_session() as s:
        assert await delivery.deliver_pending(s, now=utcnow() + timedelta(minutes=2)) == 1
    t = await _task(tid)
    assert t.delivery_status == "sent" and t.delivery_error is None and t.delivery_attempts == 2


async def test_inactive_assignee_is_marked_failed_not_emailed(org, sent):
    tid = (await _assign_one()).created[0].id
    async with async_session() as s:
        meena = await directory.get_by_email(s, "meena@example.com")
        (await s.get(Employee, meena.id)).active = False
        await s.commit()
    async with async_session() as s:
        assert await delivery.deliver_pending(s) == 0
    assert sent == [] and (await _task(tid)).delivery_status == "failed"


async def test_undelivered_task_is_not_reminded(org, monkeypatch):
    """Reminders only start once the assignment email has really gone out."""
    from app.services import followup

    async def boom(*a, **k):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(email_client, "send_email", boom)
    tid = (await _assign_one()).created[0].id
    async with async_session() as s:
        t = await s.get(Task, tid)
        t.next_followup_at = utcnow() - timedelta(hours=1)   # a reminder would be due...
        await s.commit()
    async with async_session() as s:
        stats = await followup.run_followups(s, force=True)
    assert stats["reminders"] == 0                            # ...but the task never reached the assignee
    assert (await _task(tid)).delivery_status == "waiting"


async def test_background_loop_sends_soon_after_a_task_is_created(org, sent, monkeypatch):
    """The real loop: creating a task wakes it, and the email goes out without anyone calling it."""
    monkeypatch.setattr(delivery, "_wake", asyncio.Event())     # fresh event for this test's loop
    worker = asyncio.create_task(delivery.delivery_loop())
    try:
        tid = (await _assign_one()).created[0].id
        for _ in range(50):                                      # wait up to 5 s
            await asyncio.sleep(0.1)
            if (await _task(tid)).delivery_status == "sent":
                break
        assert (await _task(tid)).delivery_status == "sent"
        assert len(sent) == 1
    finally:
        worker.cancel()


async def test_delivery_endpoint_reports_waiting_then_sent(org, sent):
    import httpx
    from httpx import ASGITransport
    from app.main import app
    tid = (await _assign_one()).created[0].id
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/tasks/delivery?ids={tid},9999")
        assert r.status_code == 200
        assert r.json() == [{"id": tid, "delivery_status": "waiting", "delivery_attempts": 0, "delivery_error": None}]
        async with async_session() as s:
            await delivery.deliver_pending(s)
        assert (await c.get(f"/tasks/delivery?ids={tid}")).json()[0]["delivery_status"] == "sent"
        assert (await c.get("/tasks/delivery?ids=abc")).status_code == 422
        assert (await c.get("/tasks/delivery?ids=")).status_code == 422
