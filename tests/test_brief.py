"""Morning brief + at-risk rules."""
import dataclasses
from datetime import datetime, timedelta

import httpx
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import select

from app import directory
from app.config import get_settings
from app.database import async_session
from app.main import app
from app.models import Task, TaskEvent
from app.services import brief, reports, risk
from app.services import tasks as task_service
from app.timeutil import utcnow

NOW = datetime(2030, 1, 9, 6, 0)          # a Wednesday, 11:30 company time


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def mk(title="Task", who="meena@example.com", due_in_days=10, **fields) -> int:
    """Create a task (email goes out immediately in the test suite) and then set any fields we want."""
    async with async_session() as s:
        person = await directory.get_by_email(s, who)
        boss = await directory.get_by_email(s, "ceo@example.com")
        t = await task_service.create_task(s, title=title, assignee=person, assigned_by=boss,
                                           due_date=NOW + timedelta(days=due_in_days) if due_in_days is not None else None)
        for k, v in fields.items():
            setattr(t, k, v)
        await s.commit()
        return t.id


async def event(task_id, kind, new_status=None, hours_ago=1):
    async with async_session() as s:
        s.add(TaskEvent(task_id=task_id, event_type=kind, new_status=new_status,
                        created_at=NOW - timedelta(hours=hours_ago)))
        await s.commit()


async def _task(tid):
    async with async_session() as s:
        return await s.get(Task, tid)


# ---------------------------------------------------------------- at-risk rules
async def test_risk_rules(org, sent):
    soon_quiet = await mk("soon, no reply", due_in_days=1, status="pending", last_reply_at=None)
    far_quiet = await mk("far, no reply", due_in_days=6, status="pending")
    soon_delayed = await mk("soon, delayed", due_in_days=1, status="delayed")
    soon_blocked = await mk("soon, blocked", due_in_days=2, status="blocked")
    nagged = await mk("two reminders", due_in_days=8, followup_count=2)
    replied = await mk("replied", due_in_days=1, status="pending", last_reply_at=NOW - timedelta(hours=1))
    done = await mk("done", due_in_days=1, status="done")
    overdue = await mk("overdue", due_in_days=-1, status="pending")
    escalated = await mk("escalated", due_in_days=1, status="pending", escalated=True)
    on_track = await mk("fine", due_in_days=1, status="on-track")

    async def reasons(tid):
        return risk.risk_reasons(await _task(tid), NOW)

    assert await reasons(soon_quiet) == ["due soon and no reply yet"]
    assert await reasons(far_quiet) == []
    assert await reasons(soon_delayed) == ["due soon but marked delayed"]
    assert await reasons(soon_blocked) == ["due soon but marked blocked"]
    assert await reasons(nagged) == ["2 reminders with no reply"]
    for tid in (replied, done, overdue, escalated, on_track):       # each of these is NOT "at risk"
        assert await reasons(tid) == [], tid


async def test_task_never_delivered_is_not_called_silent(org, sent):
    tid = await mk("not emailed yet", due_in_days=1, status="pending", notified_at=None)
    assert risk.risk_reasons(await _task(tid), NOW) == []


async def test_reports_list_at_risk_with_reasons(org, sent):
    """The dashboard reports use the real clock, so this task is dated relative to real time."""
    async with async_session() as s:
        meena = await directory.get_by_email(s, "meena@example.com")
        boss = await directory.get_by_email(s, "ceo@example.com")
        t = await task_service.create_task(s, title="heading for trouble", assignee=meena, assigned_by=boss,
                                           due_date=utcnow() + timedelta(days=1))
        t.status = "delayed"
        await s.commit()
        team_id = meena.team_id
    async with async_session() as s:
        company = await reports.company_report(s)
        team = await reports.team_report(s, team_id)
    for r in (company, team):
        assert [(x["title"], x["reasons"]) for x in r["at_risk"]] == [("heading for trouble", ["due soon but marked delayed"])]


# ---------------------------------------------------------------- the brief's numbers
async def test_brief_counts_only_the_last_24_hours(org, sent):
    a, b, c, d = await mk("a"), await mk("b"), await mk("c"), await mk("d")
    await event(a, "status_change", "done", hours_ago=3)          # counted
    await event(b, "status_change", "done", hours_ago=30)         # too old
    await event(c, "status_change", "blocked", hours_ago=2)       # new blocker
    await event(d, "escalated", hours_ago=5)
    await event(a, "created", hours_ago=4)
    async with async_session() as s:
        data = await brief.build_brief(s, now=NOW)
    assert (data["finished"], data["new_blockers"], data["escalations"], data["new_tasks"]) == (1, 1, 1, 1)
    assert data["window_label"] == "Last 24 hours"


async def test_monday_brief_looks_back_to_friday(org, sent):
    monday = datetime(2030, 1, 7, 4, 30)                          # Monday 10:00 company time
    tid = await mk("finished friday")
    async with async_session() as s:
        s.add(TaskEvent(task_id=tid, event_type="status_change", new_status="done",
                        created_at=monday - timedelta(hours=60)))   # Friday evening
        await s.commit()
        data = await brief.build_brief(s, now=monday)
    assert data["finished"] == 1 and data["window_label"] == "Since Friday"


async def test_needs_you_today_vs_at_risk(org, sent):
    overdue = await mk("overdue job", due_in_days=-3, status="pending")
    long_blocked = await mk("long blocked", due_in_days=20, status="blocked")
    await event(long_blocked, "status_change", "blocked", hours_ago=30)
    new_blocked = await mk("fresh blocker", due_in_days=1, status="blocked")
    await event(new_blocked, "status_change", "blocked", hours_ago=2)
    quiet = await mk("quiet", due_in_days=1, status="pending")
    fine = await mk("fine", due_in_days=15, status="on-track")
    async with async_session() as s:
        data = await brief.build_brief(s, now=NOW)
    needs = {i["id"]: i["reason"] for i in data["needs"]}
    risky = {i["id"]: i["reason"] for i in data["at_risk"]}
    assert set(needs) == {overdue, long_blocked}
    assert "overdue by 3 day(s)" in needs[overdue] and "blocked for 30 hour(s)" in needs[long_blocked]
    assert set(risky) == {new_blocked, quiet}                    # heading for trouble, not yet a problem
    assert fine not in needs and fine not in risky
    assert not set(needs) & set(risky)                           # a task is in one list only


async def test_done_tasks_are_ignored_and_division_counts(org, sent):
    await mk("finished", status="done", due_in_days=-5)
    bad = await mk("overdue in QA", who="meena@example.com", due_in_days=-1, status="pending")
    ok = await mk("fine in Design", who="ravi.sharma@example.com", due_in_days=9, status="on-track")
    async with async_session() as s:
        data = await brief.build_brief(s, now=NOW)
    divs = {d["name"]: d for d in data["divisions"]}
    assert divs["Quality"] == {"name": "Quality", "open": 1, "issues": 1}
    assert divs["Engineering"] == {"name": "Engineering", "open": 1, "issues": 0}
    assert data["divisions"][0]["name"] == "Quality"             # most issues first
    assert data["snapshot"]["open"] == 2


# ---------------------------------------------------------------- who gets it
async def test_recipients_are_leaders_without_a_manager(org):
    async with async_session() as s:
        assert await brief.recipients(s) == ["ceo@example.com"]   # Anita and Suresh report to the CEO


async def test_brief_to_overrides(org, monkeypatch):
    s2 = dataclasses.replace(get_settings(), brief_to="a@x.com; B@X.com , nonsense")
    monkeypatch.setattr(brief, "get_settings", lambda: s2)
    async with async_session() as s:
        assert await brief.recipients(s) == ["a@x.com", "b@x.com"]


# ---------------------------------------------------------------- the email itself
async def test_email_is_safe_and_complete(org, sent, monkeypatch):
    s2 = dataclasses.replace(get_settings(), dashboard_url="http://localhost:8000/dashboard")
    monkeypatch.setattr(brief, "get_settings", lambda: s2)
    await mk("<script>alert(1)</script> overdue", due_in_days=-2, status="pending")
    async with async_session() as s:
        subject, text, html = brief.render_brief(await brief.build_brief(s, now=NOW))
    assert "<script>" not in html and "&lt;script&gt;" in html            # titles can never inject HTML
    assert subject.startswith("Work status brief")
    assert "NEEDS YOU TODAY" in text and "AT RISK" in text and "BY DIVISION" in text
    assert "http://localhost:8000/dashboard" in text and "http://localhost:8000/dashboard" in html


async def test_empty_company_says_all_clear(org):
    async with async_session() as s:
        _, text, html = brief.render_brief(await brief.build_brief(s, now=NOW))
    assert "Nothing - no overdue" in text and "Nothing at risk" in text and "Nothing at risk" in html


# ---------------------------------------------------------------- sending
async def test_send_brief_emails_the_ceo_once(org, sent, llm):
    sent.clear()
    async with async_session() as s:
        out = await brief.send_brief(s)
    assert out["results"] == [{"to": "ceo@example.com", "ok": True, "error": None}]
    assert len(sent) == 1 and sent[0]["to"] == "ceo@example.com" and sent[0]["html"]
    assert llm.calls == 0                                         # the brief never uses the AI


async def test_send_failure_is_reported_per_person(org, monkeypatch):
    from app import email_client

    async def boom(*a, **k):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(email_client, "send_email", boom)
    async with async_session() as s:
        out = await brief.send_brief(s)
    assert out["results"][0]["ok"] is False and "smtp down" in out["results"][0]["error"]


async def test_nobody_to_send_to(monkeypatch):
    async with async_session() as s:
        out = await brief.send_brief(s)
    assert out["results"] == [] and "Nobody to send to" in out["message"]


async def test_endpoints(client, org, sent):
    sent.clear()
    p = (await client.get("/agent/brief/preview")).json()
    assert p["recipients"] == ["ceo@example.com"] and "<html" in p["html"].lower() and sent == []   # preview sends nothing
    r = (await client.post("/agent/brief/send")).json()
    assert r["results"][0]["ok"] is True and len(sent) == 1


async def test_daily_job_is_registered_for_working_days(org):
    from app import scheduler as sch
    sch.start_scheduler()
    try:
        job = sch.scheduler.get_job("brief")
        assert job is not None
        assert "mon-fri" in str(job.trigger) and "hour='9'" in str(job.trigger)
    finally:
        sch.shutdown_scheduler()
        sch.scheduler.shutdown(wait=False)


async def test_a_blocked_task_that_gets_done_leaves_the_brief(org, sent):
    """The brief is rebuilt from the database each time, so it always shows today's truth."""
    tid = await mk("vendor drawings", due_in_days=20, status="blocked")
    await event(tid, "status_change", "blocked", hours_ago=30)
    async with async_session() as s:
        before = await brief.build_brief(s, now=NOW)
    assert [i["id"] for i in before["needs"]] == [tid] and before["snapshot"]["blocked"] == 1

    async with async_session() as s:                       # the employee finishes it
        t = await s.get(Task, tid)
        t.status = "done"
        s.add(TaskEvent(task_id=tid, event_type="status_change", new_status="done",
                        created_at=NOW - timedelta(hours=1)))
        await s.commit()
    async with async_session() as s:
        after = await brief.build_brief(s, now=NOW)
    assert after["needs"] == [] and after["at_risk"] == []           # gone from every list
    assert after["snapshot"]["blocked"] == 0 and after["snapshot"]["open"] == 0
    assert after["finished"] == 1                                    # and counted as finished
    assert after["new_blockers"] == 0                                # (the old blocked event is >24h old)
