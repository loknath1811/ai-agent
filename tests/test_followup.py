from datetime import datetime, timedelta

from sqlalchemy import select

from app import directory
from app.database import async_session
from app.llm_tasks import AssignmentItem
from app.models import Task, TaskEvent
from app.services import followup, inbox
from app.services import tasks as ts
from app.services.assign import assign_from_items
from app.timeutil import utcnow
from tests.conftest import make_email


async def _assign(due_in_days=10):
    async with async_session() as s:
        ceo = await directory.get_by_email(s, "ceo@example.com")
        due = (utcnow() + timedelta(days=due_in_days)).strftime("%Y-%m-%d") if due_in_days is not None else None
        res = await assign_from_items(s, ceo, [AssignmentItem(title="Audit", assignee="Ravi Kumar", due_date=due)])
        return res.created[0].id


async def _run(now):
    async with async_session() as s:
        return await followup.run_followups(s, now=now, force=True)


def test_cadence_gets_tighter_near_deadline():
    now = utcnow()
    assert ts.followup_gap_hours(None, now) == 72
    assert ts.followup_gap_hours(now + timedelta(days=20), now) == 72
    assert ts.followup_gap_hours(now + timedelta(days=4), now) == 48
    assert ts.followup_gap_hours(now + timedelta(hours=30), now) == 24
    assert ts.followup_gap_hours(now - timedelta(days=1), now) == 24


async def test_reminders_then_escalation_then_reply_resets(org, sent, llm, mailbox):
    tid = await _assign(60)          # far deadline, so only 'silence' can trigger escalation
    sent.clear()
    now = utcnow()

    for i in range(1, 4):                                   # 3 unanswered reminders
        now += timedelta(days=4)
        stats = await _run(now)
        assert stats["reminders"] == 1 and stats["escalated_silence"] == 0
        async with async_session() as s:
            assert (await s.get(Task, tid)).followup_count == i
    assert all(m["to"] == "ravi.kumar@example.com" for m in sent) and len(sent) == 3

    sent.clear()
    now += timedelta(days=4)                                # 4th time: escalate instead
    stats = await _run(now)
    assert stats["escalated_silence"] == 1 and stats["reminders"] == 0
    assert {m["to"] for m in sent} == {"ceo@example.com", "anita@example.com"}
    async with async_session() as s:
        t = await s.get(Task, tid)
        assert t.escalated and t.next_followup_at is None and t.status == "pending"  # NOT 'blocked'

    sent.clear()
    assert (await _run(now + timedelta(days=30)))["reminders"] == 0   # no more nagging after escalation

    mailbox.emails = [make_email("ravi.kumar@example.com", f"Re: [Task #{tid}]", "sorry, going fine")]
    await inbox.process_inbox()                             # a reply clears it
    async with async_session() as s:
        t = await s.get(Task, tid)
        assert not t.escalated and t.followup_count == 0 and t.status == "on-track"
        assert t.next_followup_at is not None


async def test_overdue_task_is_escalated_exactly_once(org, sent):
    tid = await _assign(1)
    sent.clear()
    now = utcnow() + timedelta(days=3)                      # 2 days past the deadline

    async def escalation_events():
        async with async_session() as s:
            res = await s.execute(select(TaskEvent).where(TaskEvent.task_id == tid, TaskEvent.event_type == "escalated"))
            return len(res.scalars().all())

    await _run(now)
    assert await escalation_events() == 1
    assert {m["to"] for m in sent if "ESCALATION" in m["subject"]} == {"ceo@example.com", "anita@example.com"}
    sent.clear()
    await _run(now + timedelta(hours=1))                    # next pass: nothing new
    assert await escalation_events() == 1 and sent == []
    async with async_session() as s:
        t = await s.get(Task, tid)
        assert t.escalated and t.status == "pending"        # silence is not 'blocked'


async def test_done_tasks_are_left_alone(org, sent):
    tid = await _assign(1)
    async with async_session() as s:
        t = await s.get(Task, tid)
        await ts.apply_status(s, t, "done", detail="finished")
        await s.commit()
    sent.clear()
    stats = await _run(utcnow() + timedelta(days=30))
    assert stats["reminders"] == 0 and stats["escalated_overdue"] == 0 and sent == []


async def test_no_follow_ups_outside_working_hours(org, sent):
    await _assign(10)
    sent.clear()
    sunday = datetime(2026, 10, 4, 6, 0)                    # Sunday, 11:30 IST
    async with async_session() as s:
        stats = await followup.run_followups(s, now=sunday)  # force=False
    assert stats["skipped"] == "outside working hours" and sent == []


async def test_failed_assignment_email_is_retried(org, monkeypatch):
    from app import email_client
    calls = {"n": 0}

    async def flaky(to, subject, body, cc=None, in_reply_to=None, task_id=None, html=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("smtp down")
        return f"<m{calls['n']}@t>"
    monkeypatch.setattr(email_client, "send_email", flaky)
    tid = await _assign(10)
    async with async_session() as s:
        assert (await s.get(Task, tid)).notified_at is None
    stats = await _run(utcnow() + timedelta(minutes=2))   # first retry is due after 1 minute
    assert stats["retried_delivery"] == 1
    async with async_session() as s:
        assert (await s.get(Task, tid)).notified_at is not None


async def test_reminders_and_ack_are_sent_in_the_same_thread(org, sent):
    tid = await _assign(10)
    assignment = next(m for m in sent if m["to"] == "ravi.kumar@example.com")
    assert assignment["in_reply_to"] is None                      # the first email starts the thread
    assert assignment["subject"] == f"[Task #{tid}] Audit"

    sent.clear()
    await _run(utcnow() + timedelta(days=4))                      # first reminder is due
    reminder = next(m for m in sent if m["to"] == "ravi.kumar@example.com")
    assert reminder["in_reply_to"] == assignment["mid"]           # reply to the assignment
    assert reminder["subject"] == f"Re: [Task #{tid}] Audit"      # same subject, so mail apps group it

    sent.clear()
    await _run(utcnow() + timedelta(days=8))                      # second reminder replies to the first
    second = next(m for m in sent if m["to"] == "ravi.kumar@example.com")
    assert second["in_reply_to"] == reminder["mid"]


async def test_escalation_is_not_threaded_with_the_assignee(org, sent):
    tid = await _assign(10)
    sent.clear()
    async with async_session() as s:
        task = await ts.get_task(s, tid)
        from app.services import escalation
        await escalation.escalate(s, task, "test")
        await s.commit()
    assert sent and all(m["in_reply_to"] is None for m in sent)


async def test_assigner_is_copied_on_the_assignment_email(org, monkeypatch):
    from app import email_client
    seen, counter = [], iter(range(1, 1000))

    async def capture(to, subject, body, cc=None, in_reply_to=None, task_id=None, html=None):
        seen.append({"to": to, "cc": cc})
        return f"<m{next(counter)}@t>"      # never repeats, even after seen.clear()
    monkeypatch.setattr(email_client, "send_email", capture)

    await _assign(10)                                   # the CEO assigns work to Ravi Kumar
    assert seen[0]["to"] == "ravi.kumar@example.com"
    assert seen[0]["cc"] == ["ceo@example.com"]         # the CEO is copied

    seen.clear()
    async with async_session() as s:                    # Ravi assigns something to himself
        ravi = await directory.get_by_email(s, "ravi.kumar@example.com")
        await ts.create_task(s, title="Own task", assignee=ravi, assigned_by=ravi)
    assert seen[0]["cc"] is None                        # nobody to copy

    seen.clear()
    await _run(utcnow() + timedelta(days=4))            # a reminder to Ravi's first task
    assert seen and all(not m["cc"] for m in seen)       # reminders are not copied