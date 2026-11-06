"""When the AI is unsure of a status: ask the employee, hold the alerts, accept 'yes' or a correction,
and let a leader fix a wrong status. Plus the accuracy numbers."""
import dataclasses
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import select

from app import directory, llm_tasks
from app.config import get_settings
from app.database import async_session
from app.llm_tasks import AssignmentItem
from app.main import app
from app.models import EmailLog, OutboundEmail, Task, TaskEvent
from app.services import followup, impact, inbox
from app.services.assign import assign_from_items
from app.timeutil import utcnow
from tests.conftest import make_email


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def assign(title="Audit", assignee="Meena", by="ceo@example.com"):
    async with async_session() as s:
        res = await assign_from_items(s, await directory.get_by_email(s, by),
                                      [AssignmentItem(title=title, assignee=assignee, due_date="2030-01-10")])
        return res.created[0].id


async def get(tid):
    async with async_session() as s:
        return await s.get(Task, tid)


async def kinds(tid):
    async with async_session() as s:
        return [k for (k,) in (await s.execute(select(OutboundEmail.kind).where(OutboundEmail.task_id == tid)
                                               .order_by(OutboundEmail.sent_at, OutboundEmail.message_id))).all()]


async def events(tid):
    async with async_session() as s:
        return [(e.event_type, e.new_status) for e in
                (await s.execute(select(TaskEvent).where(TaskEvent.task_id == tid).order_by(TaskEvent.id))).scalars()]


async def reply(mailbox, tid, body, frm="meena@example.com"):
    mailbox.emails = [make_email(frm, f"Re: [Task #{tid}] Audit", body)]
    return await inbox.process_inbox()


VAGUE = "Stuck on the vendor, not sure how long this will take"


# ---------------------------------------------------------------- the AI part
def test_confidence_is_read_and_odd_values_do_not_break_the_reply():
    parse = lambda **kw: llm_tasks.ReplyAnalysis(status="blocked", **kw)
    assert parse().confidence == "high"                      # a model that omits it: nothing changes
    assert parse(confidence="LOW").confidence == "low"
    assert parse(confidence="very high").confidence == "medium"
    assert parse(confidence="85%").confidence == "medium"
    assert parse(confidence=None).confidence == "medium"


# ---------------------------------------------------------------- plain yes
@pytest.mark.parametrize("text,expected", [
    ("yes", True), ("Yes.", True), ("Yes, that's right", True), ("yep", True), ("Correct!", True),
    ("ok", True), ("Yes that is right thanks", False),            # 5 words: let the AI read it
    ("yes but it will be late", False), ("yes, blocked", False), ("no", False),
    ("not right", False), ("it's done", False), ("", False), ("Thanks", False),
])
def test_is_confirmation(text, expected):
    assert inbox.is_confirmation(text) is expected


def test_is_confirmation_only_looks_at_the_first_line():
    assert inbox.is_confirmation("Yes\n\nSent from my phone") is True
    assert inbox.is_confirmation("Hi,\nyes") is False


# ---------------------------------------------------------------- confident replies: nothing new happens
async def test_confident_reply_behaves_exactly_as_before(org, sent, llm, mailbox):
    tid = await assign()
    await reply(mailbox, tid, "We are blocked waiting for the vendor part")
    t = await get(tid)
    assert t.status == "blocked" and t.status_check is None
    assert "blocker" in await kinds(tid) and "check" not in await kinds(tid)
    assert "ceo@example.com" in [m["to"] for m in sent]               # the alert went out straight away


# ---------------------------------------------------------------- unsure replies: ask, and hold the alerts
async def test_unsure_reply_is_saved_provisionally_and_the_employee_is_asked(org, sent, llm, mailbox):
    llm.confidence = "low"
    tid = await assign()
    sent.clear()
    await reply(mailbox, tid, VAGUE)
    t = await get(tid)
    assert t.status == "blocked" and t.status_check == "unconfirmed"             # shown, but marked unconfirmed
    assert ("status_check_asked", None) in await events(tid)
    assert [m["to"] for m in sent] == ["meena@example.com"]                     # only the employee is emailed...
    assert "Is that right?" in sent[0]["body"] and "I understood your status as" in sent[0]["body"]
    ks = await kinds(tid)
    assert ks.count("check") == 1 and "blocker" not in ks and "ack" not in ks   # ...and no manager alert yet


async def test_yes_confirms_without_calling_the_ai_and_releases_the_held_alert(org, sent, llm, mailbox):
    llm.confidence = "low"
    tid = await assign()
    await reply(mailbox, tid, VAGUE)
    calls, sent_before = llm.calls, len(sent)
    stats = await reply(mailbox, tid, "Yes")
    assert stats["processed"] == 1 and llm.calls == calls                       # a plain yes needs no AI
    t = await get(tid)
    assert t.status == "blocked" and t.status_check is None
    assert ("status_confirmed", None) in await events(tid)
    new_to = [m["to"] for m in sent[sent_before:]]
    assert "ceo@example.com" in new_to and "anita@example.com" in new_to        # the blocker alert goes out now
    assert "meena@example.com" in new_to                                         # and the employee is thanked


async def test_a_different_answer_corrects_the_status_and_runs_the_right_alerts(org, sent, llm, mailbox):
    llm.confidence = "low"
    tid = await assign()
    await reply(mailbox, tid, VAGUE)
    sent_before = len(sent)
    await reply(mailbox, tid, "Actually it is all finished, report attached")
    t = await get(tid)
    assert t.status == "done" and t.status_check is None and t.completed_at is not None
    assert ("status_corrected", None) in await events(tid)
    assert "blocker" not in await kinds(tid)                                    # the wrong alert was never sent
    assert "ceo@example.com" in [m["to"] for m in sent[sent_before:]]           # the "done" notice is


async def test_yes_but_is_not_mistaken_for_a_plain_yes(org, sent, llm, mailbox):
    llm.confidence = "low"
    tid = await assign()
    await reply(mailbox, tid, VAGUE)
    calls = llm.calls
    await reply(mailbox, tid, "Yes but it will be late")
    assert llm.calls == calls + 1                                               # went to the AI
    assert (await get(tid)).status == "delayed"
    assert ("status_corrected", None) in await events(tid)


async def test_an_empty_answer_leaves_the_question_open(org, sent, llm, mailbox):
    llm.confidence = "low"
    tid = await assign()
    await reply(mailbox, tid, VAGUE)
    await reply(mailbox, tid, "thanks")
    t = await get(tid)
    assert t.status == "blocked" and t.status_check == "unconfirmed"


async def test_we_never_ask_twice_for_the_same_task(org, sent, llm, mailbox):
    llm.confidence = "low"
    tid = await assign()
    await reply(mailbox, tid, VAGUE)
    await reply(mailbox, tid, "we will be late I think, not sure")             # still unsure - but this settles it
    t = await get(tid)
    assert t.status == "delayed" and t.status_check is None
    assert (await kinds(tid)).count("check") == 1


async def test_the_feature_can_be_switched_off(org, sent, llm, mailbox, monkeypatch):
    llm.confidence = "low"
    monkeypatch.setattr(inbox, "get_settings", lambda: dataclasses.replace(get_settings(), confirm_low_confidence=False))
    tid = await assign()
    await reply(mailbox, tid, VAGUE)
    assert (await get(tid)).status_check is None
    assert "check" not in await kinds(tid) and "blocker" in await kinds(tid)


# ---------------------------------------------------------------- nobody answers
async def test_unanswered_question_expires_and_the_held_alert_is_sent(org, sent, llm, mailbox):
    llm.confidence = "low"
    tid = await assign()
    await reply(mailbox, tid, VAGUE)
    async with async_session() as s:
        early = await followup.run_followups(s, now=utcnow() + timedelta(hours=23), force=True)
    assert early["status_checks_expired"] == 0 and (await get(tid)).status_check == "unconfirmed"
    assert "blocker" not in await kinds(tid)
    async with async_session() as s:
        late = await followup.run_followups(s, now=utcnow() + timedelta(hours=25), force=True)
    assert late["status_checks_expired"] == 1
    t = await get(tid)
    assert t.status == "blocked" and t.status_check is None
    assert ("status_check_expired", None) in await events(tid)
    assert "blocker" in await kinds(tid)                                        # the alert is not lost forever


# ---------------------------------------------------------------- a leader corrects a status
async def set_status(tid, status, check=None):
    async with async_session() as s:
        t = await s.get(Task, tid)
        t.status, t.status_check = status, check
        await s.commit()


async def test_assigner_can_correct_and_the_assignee_is_told(client, org, sent):
    tid = await assign()
    await set_status(tid, "blocked")
    sent.clear()
    r = await client.post(f"/tasks/{tid}/correct", json={"status": "on-track", "as_email": "ceo@example.com"})
    assert r.status_code == 200 and r.json()["status"] == "on-track" and r.json()["status_check"] is None
    assert ("status_corrected", None) in await events(tid)
    assert [m["to"] for m in sent] == ["meena@example.com"]
    assert "Test CEO changed the recorded status" in sent[0]["body"] and "blocked" in sent[0]["body"]
    assert "correction_notice" in await kinds(tid)


async def test_a_leader_above_the_assignee_can_correct_too(client, org, sent):
    tid = await assign()
    r = await client.post(f"/tasks/{tid}/correct", json={"status": "delayed", "as_email": "anita@example.com"})
    assert r.status_code == 200                                                 # Anita is Meena's manager


async def test_others_cannot_correct(client, org, sent):
    tid = await assign()
    for who in ("suresh@example.com", "ravi.kumar@example.com", "meena@example.com", "nobody@example.com", None):
        r = await client.post(f"/tasks/{tid}/correct", json={"status": "done", "as_email": who})
        assert r.status_code == 403, who
    assert (await get(tid)).status == "pending"


async def test_confirming_an_unconfirmed_status_as_a_leader(client, org, sent):
    tid = await assign()
    await set_status(tid, "blocked", "unconfirmed")
    r = await client.post(f"/tasks/{tid}/correct", json={"status": "blocked", "as_email": "ceo@example.com"})
    assert r.status_code == 200 and r.json()["status_check"] is None
    assert ("status_confirmed", None) in await events(tid)


async def test_correction_edge_cases(client, org, sent):
    tid = await assign()
    same = await client.post(f"/tasks/{tid}/correct", json={"status": "pending", "as_email": "ceo@example.com"})
    assert same.status_code == 409                                              # nothing to correct
    bad = await client.post(f"/tasks/{tid}/correct", json={"status": "finished-ish", "as_email": "ceo@example.com"})
    assert bad.status_code == 422
    missing = await client.post("/tasks/9999/correct", json={"status": "done", "as_email": "ceo@example.com"})
    assert missing.status_code == 404


async def test_task_data_exposes_the_unconfirmed_mark(client, org, sent):
    tid = await assign()
    await set_status(tid, "blocked", "unconfirmed")
    assert (await client.get(f"/tasks/{tid}")).json()["status_check"] == "unconfirmed"


# ---------------------------------------------------------------- the accuracy numbers
async def test_accuracy_numbers(org, sent, llm, mailbox):
    llm.confidence = "low"
    right = await assign("one")
    wrong = await assign("two")
    silent = await assign("three")
    for tid in (right, wrong, silent):
        await reply(mailbox, tid, VAGUE)
    await reply(mailbox, right, "yes")
    await reply(mailbox, wrong, "it's finished actually")
    async with async_session() as s:
        a = (await impact.compute(s, days=7))["accuracy"]
    assert a["asked"] == 3 and a["confirmed"] == 1 and a["corrected"] == 1
    assert a["checked"] == 2 and a["right_pct"] == 50 and a["waiting_now"] == 1 and a["expired"] == 0


async def test_accuracy_is_none_until_something_is_checked(org):
    async with async_session() as s:
        a = (await impact.compute(s, days=7))["accuracy"]
    assert a["right_pct"] is None and a["checked"] == 0 and a["waiting_now"] == 0
