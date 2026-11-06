from sqlalchemy import select

from app import directory
from app.database import async_session
from app.llm_tasks import AssignmentItem
from app.models import EmailLog, Task, TaskEvent
from app.services import inbox
from app.services.assign import assign_from_items
from tests.conftest import make_email


async def _assign(title, assignee, by="ceo@example.com", due=None):
    async with async_session() as s:
        res = await assign_from_items(s, await directory.get_by_email(s, by),
                                      [AssignmentItem(title=title, assignee=assignee, due_date=due)])
        return res.created[0].id


async def _task(tid):
    async with async_session() as s:
        return await s.get(Task, tid)


async def test_reply_with_quoted_history_marks_done_and_notifies(org, sent, llm, mailbox):
    tid = await _assign("NCR review", "Ravi Kumar")
    sent.clear()
    mailbox.emails = [make_email(
        "ravi.kumar@example.com", f"Re: [Task #{tid}] NCR review",
        "Finished the review, report attached.")]
    stats = await inbox.process_inbox()
    assert stats == {"fetched": 1, "processed": 1, "ignored": 0, "failed": 0}
    t = await _task(tid)
    assert t.status == "done" and t.completed_at is not None and t.next_followup_at is None
    to = [m["to"] for m in sent]
    assert "ceo@example.com" in to                 # assigner told it is done
    assert "ravi.kumar@example.com" in to          # acknowledgement
    assert len(mailbox.read) == 1


async def test_reply_without_task_number_matches_single_open_task(org, sent, llm, mailbox):
    tid = await _assign("Audit", "Meena")
    mailbox.emails = [make_email("meena@example.com", "quick update", "Going fine, 50% complete")]
    await inbox.process_inbox()
    assert (await _task(tid)).status == "on-track"


async def test_thread_headers_match_even_if_subject_changed(org, sent, llm, mailbox):
    tid = await _assign("Audit", "Meena")
    await _assign("Other", "Meena")                # two open tasks, so no 'single task' shortcut
    mid = sent[0]["mid"]                           # our outgoing assignment email
    mailbox.emails = [make_email("meena@example.com", "totally different subject",
                                 "all finished", in_reply_to=mid)]
    await inbox.process_inbox()
    assert (await _task(tid)).status == "done"


async def test_unknown_sender_and_autoreplies_are_ignored(org, sent, llm, mailbox):
    tid = await _assign("Audit", "Meena")
    mailbox.emails = [
        make_email("stranger@evil.com", f"Re: [Task #{tid}]", "done"),
        make_email("meena@example.com", f"Re: [Task #{tid}]", "Out of office", auto_generated=True),
    ]
    stats = await inbox.process_inbox()
    assert stats["ignored"] == 2 and stats["processed"] == 0
    assert (await _task(tid)).status == "pending"


async def test_non_assignee_cannot_change_status(org, sent, llm, mailbox):
    tid = await _assign("Audit", "Meena")
    mailbox.emails = [make_email("ravi.kumar@example.com", f"Re: [Task #{tid}]", "it is all done")]
    await inbox.process_inbox()
    assert (await _task(tid)).status == "pending"
    async with async_session() as s:
        kinds = [e.event_type for e in (await s.execute(select(TaskEvent).where(TaskEvent.task_id == tid))).scalars()]
        assert "comment" in kinds


async def test_blocked_notifies_assigner_and_manager(org, sent, llm, mailbox):
    tid = await _assign("Audit", "Ravi Kumar")
    sent.clear()
    mailbox.emails = [make_email("ravi.kumar@example.com", f"Re: [Task #{tid}]", "I am blocked, waiting for the vendor")]
    await inbox.process_inbox()
    assert (await _task(tid)).status == "blocked"
    blocker_mails = [m for m in sent if "BLOCKED" in m["subject"]]
    assert {m["to"] for m in blocker_mails} == {"ceo@example.com", "anita@example.com"}


async def test_several_open_tasks_ai_picks_or_we_ask(org, sent, llm, mailbox):
    t1 = await _assign("Audit report", "Meena")
    t2 = await _assign("Training deck", "Meena")
    sent.clear()
    # AI can tell -> applied to that task
    llm.pick_id = t2
    mailbox.emails = [make_email("meena@example.com", "hello", "deck is finished")]
    await inbox.process_inbox()
    assert (await _task(t2)).status == "done" and (await _task(t1)).status == "pending"
    # AI cannot tell (she again has two open tasks) -> we ask, nothing changes
    await _assign("Safety checklist", "Meena")
    llm.pick_id = None
    sent.clear()
    mailbox.emails = [make_email("meena@example.com", "hello again", "just checking in")]
    await inbox.process_inbox()
    assert any("could not tell which" in m["body"] for m in sent)


async def test_leader_email_creates_tasks_and_confirms(org, sent, llm, mailbox):
    llm.assign_json = {"is_assignment": True, "items": [
        {"title": "Complete NCR root-cause review", "assignee": "QA team",
         "due_date": "2030-01-10", "fan_out": False}]}
    mailbox.emails = [make_email("ceo@example.com", "Please act",
                                 "Ask the QA team to complete the NCR root-cause review by Friday")]
    stats = await inbox.process_inbox()
    assert stats["processed"] == 1
    async with async_session() as s:
        t = (await s.execute(select(Task))).scalars().first()
        assert t.assignee.email == "anita@example.com" and t.assigned_by.email == "ceo@example.com"
    assert {m["to"] for m in sent} == {"anita@example.com", "ceo@example.com"}  # assignment + confirmation


async def test_non_leader_cannot_assign_by_email(org, sent, llm, mailbox):
    llm.assign_json = {"is_assignment": True, "items": [{"title": "X", "assignee": "Meena"}]}
    mailbox.emails = [make_email("ravi.kumar@example.com", "do this", "Ask Meena to do X")]
    await inbox.process_inbox()
    async with async_session() as s:
        assert (await s.execute(select(Task))).scalars().first() is None


async def test_nothing_is_processed_twice(org, sent, llm, mailbox):
    tid = await _assign("Audit", "Meena")
    mailbox.emails = [make_email("meena@example.com", f"Re: [Task #{tid}]", "finished", uid=77)]
    await inbox.process_inbox()
    calls = llm.calls
    stats = await inbox.process_inbox()           # same unread email shows up again
    assert stats["fetched"] == 0 and llm.calls == calls


async def test_ai_failure_is_retried_then_given_up(org, sent, llm, mailbox):
    tid = await _assign("Audit", "Meena")
    llm.fail = True
    mailbox.emails = [make_email("meena@example.com", f"Re: [Task #{tid}]", "finished", uid=88)]
    for _ in range(3):
        stats = await inbox.process_inbox()
        assert stats["failed"] == 1
    assert (await inbox.process_inbox())["fetched"] == 0   # stops after IMAP_MAX_ATTEMPTS
    assert (await _task(tid)).status == "pending"
    llm.fail = False
    async with async_session() as s:
        row = (await s.execute(select(EmailLog).where(EmailLog.uid == 88))).scalars().first()
        assert row.status == "failed" and row.attempts == 3


async def test_proposed_new_deadline_is_recorded(org, sent, monkeypatch, mailbox):
    from app import llm_tasks
    tid = await _assign("Audit", "Meena", due="2030-01-01")

    async def fake(system, user):
        return '{"status":"delayed","summary":"need more time","new_due_date":"2030-01-15"}'
    monkeypatch.setattr(llm_tasks, "_invoke", fake)
    mailbox.emails = [make_email("meena@example.com", f"Re: [Task #{tid}]", "need more time, can do by 15 Jan")]
    await inbox.process_inbox()
    t = await _task(tid)
    assert t.status == "delayed" and t.due_date.day == 15
