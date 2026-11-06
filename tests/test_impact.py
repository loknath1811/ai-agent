"""Impact numbers: every figure is checked against a hand-built history."""
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport

from app import directory
from app.database import async_session
from app.main import app
from app.models import EmailLog, OutboundEmail, Task, TaskEvent
from app.services import impact
from app.timeutil import utcnow

BASE = utcnow()


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


def ago(**kw):
    return BASE - timedelta(**kw)


async def person(email="meena@example.com"):
    async with async_session() as s:
        return (await directory.get_by_email(s, email)).id


async def mk_task(status="pending", due=None, completed_at=None, who="meena@example.com") -> int:
    async with async_session() as s:
        t = Task(title="t", assignee_id=await person(who), assigned_by_id=await person("ceo@example.com"),
                 status=status, due_date=due, completed_at=completed_at, created_at=ago(days=1))
        s.add(t)
        await s.commit()
        return t.id


async def ev(task_id, kind, when, actor=None, new_status=None):
    async with async_session() as s:
        s.add(TaskEvent(task_id=task_id, event_type=kind, actor_id=actor, new_status=new_status, created_at=when))
        await s.commit()


async def run(days=7):
    async with async_session() as s:
        return await impact.compute(s, days=days, now=BASE)


async def test_empty_database_gives_zeros_and_no_crash(org):
    r = await run()
    assert r["tasks"]["assigned"] == 0 and r["tasks"]["on_time_pct"] is None
    assert r["speed"]["first_reply_median_hours"] is None and r["speed"]["blocker_alert_median_seconds"] is None
    assert r["time_saved"]["minutes"] == 0


async def test_period_filter(org):
    a, b, c = await mk_task(), await mk_task(), await mk_task()
    await ev(a, "created", ago(days=2))
    await ev(b, "created", ago(days=6))
    await ev(c, "created", ago(days=20))
    assert (await run(7))["tasks"]["assigned"] == 2
    assert (await run(30))["tasks"]["assigned"] == 3
    assert (await run(1))["tasks"]["assigned"] == 0


async def test_on_time_completion(org):
    await mk_task("done", due=ago(days=1), completed_at=ago(days=2))     # finished before the deadline
    await mk_task("done", due=ago(days=3), completed_at=ago(days=2))     # finished after the deadline
    await mk_task("done", due=None, completed_at=ago(days=2))            # no deadline: cannot be judged
    await mk_task("done", due=ago(days=40), completed_at=ago(days=30))   # outside the period
    await mk_task("pending")
    t = (await run(7))["tasks"]
    assert (t["completed"], t["completed_with_deadline"], t["completed_on_time"], t["on_time_pct"]) == (3, 2, 1, 50)
    assert t["open_now"] == 1


async def test_emails_the_agent_sent_and_replies_it_read(org):
    tid = await mk_task()
    async with async_session() as s:
        for i, (kind, when) in enumerate([("reminder", ago(days=1)), ("reminder", ago(days=2)), ("reminder", ago(days=30)),
                                           ("blocker", ago(days=1)), ("assignment", ago(days=1))]):
            s.add(OutboundEmail(message_id=f"<m{i}@t>", task_id=tid, to_email="x@y.z", kind=kind, sent_at=when))
        for status, detail, when in [
            ("processed", "task 5: blocked (changed)", ago(hours=5)),
            ("processed", "task 6: reply had no status", ago(hours=6)),
            ("processed", "leader assignment: 2 created, 0 problems", ago(hours=7)),
            ("processed", "comment on task 7 from non-assignee a@b.c", ago(hours=8)),      # a manager comment: not a reply
            ("ignored", "task 8: ignored", ago(hours=9)),                                   # not processed
            ("processed", "task 9: done (changed)", ago(days=30)),                         # outside the period
        ]:
            s.add(EmailLog(status=status, detail=detail, created_at=when))
        await s.commit()
    a = (await run(7))["automation"]
    assert a["reminders_sent"] == 2 and a["blocker_alert_emails"] == 1 and a["emails_sent_total"] == 4
    assert a["replies_read"] == 2 and a["assignments_received_by_email"] == 1


async def test_time_to_first_reply(org):
    meena, anita = await person("meena@example.com"), await person("anita@example.com")
    t1, t2, t3, t4 = [await mk_task(who="meena@example.com") for _ in range(4)]
    for t in (t1, t2, t3, t4):
        await ev(t, "created", ago(hours=48))
    await ev(t1, "status_change", ago(hours=48) + timedelta(hours=2), actor=meena, new_status="on-track")
    await ev(t2, "status_change", ago(hours=48) + timedelta(hours=10), actor=meena, new_status="done")
    await ev(t2, "status_change", ago(hours=48) + timedelta(hours=11), actor=meena, new_status="done")   # later reply: ignored
    await ev(t3, "comment", ago(hours=48) + timedelta(hours=1), actor=anita)                          # a manager's comment: not the assignee
    await ev(t3, "comment", ago(hours=48) + timedelta(hours=4), actor=meena)
    # t4 never answered
    sp = (await run(7))["speed"]
    assert sp["first_reply_samples"] == 3 and sp["first_reply_median_hours"] == 4.0     # 2h, 4h, 10h
    assert sp["tasks_assigned_in_period"] == 4 and sp["replied_pct"] == 75


async def test_blocker_alert_speed(org):
    a, b, c = await mk_task(), await mk_task(), await mk_task()
    await ev(a, "status_change", ago(hours=3), new_status="blocked")
    await ev(a, "blocker_reported", ago(hours=3) + timedelta(seconds=6))
    await ev(b, "status_change", ago(hours=2), new_status="blocked")
    await ev(b, "blocker_reported", ago(hours=2) + timedelta(seconds=10))
    await ev(c, "blocker_reported", ago(hours=1))                                          # no matching blocked status: skipped
    sp = (await run(7))["speed"]
    assert sp["blocker_alert_samples"] == 2 and sp["blocker_alert_median_seconds"] == 8.0
    assert sp["mailbox_check_seconds"] > 0                                                 # the separate wait is reported too


async def test_time_saved_is_steps_times_the_stated_assumption(org):
    tid = await mk_task()
    async with async_session() as s:
        for i in range(2):
            s.add(OutboundEmail(message_id=f"<r{i}@t>", task_id=tid, to_email="x@y.z", kind="reminder", sent_at=ago(hours=1)))
        for i in range(3):
            s.add(EmailLog(status="processed", detail=f"task {i}: on-track", created_at=ago(hours=2)))
        await s.commit()
    await ev(tid, "blocker_reported", ago(hours=1))
    await ev(tid, "escalated", ago(hours=1))
    ts = (await run(7))["time_saved"]
    assert ts["steps"] == 7 and ts["minutes_per_step"] == 3 and ts["minutes"] == 21 and ts["hours"] == 0.3     # 21 min = 0.35 h, shown to one decimal
    assert "each counted as one manual step" in ts["basis"]


async def test_endpoint(client, org):
    r = await client.get("/reports/impact?days=30")
    assert r.status_code == 200 and r.json()["days"] == 30 and set(r.json()) >= {"tasks", "automation", "speed", "time_saved"}
    assert (await client.get("/reports/impact")).json()["days"] == 7
    for bad in ("0", "-1", "3651", "abc"):
        assert (await client.get(f"/reports/impact?days={bad}")).status_code == 422
