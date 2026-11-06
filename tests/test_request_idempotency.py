"""Two fixes found by a real test (the same chat message sent twice):

1. REQUEST-level idempotency. The first fix compared what the AI produced (title + deadline), but the AI words
   them differently each time ("Finish..." vs "Complete...", 12 Nov vs 11 Nov). So we now ALSO remember what the
   person actually typed (chat) or the email's own Message-ID (email), which never varies.
2. WORKING DAYS are counted by code. The AI only reports the number ("4"); it got the date wrong once.
"""
import asyncio
import json
from datetime import date, timedelta

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app import agent, directory, llm_tasks
from app.database import async_session
from app.models import OutboundEmail, Task
from app.services import idempotency, inbox
from app.timeutil import add_working_days

from .conftest import make_email
from .test_agent import RecordingFake, _script


def call(instruction, call_id="1"):
    return {"name": "assign_work", "id": call_id, "args": {"instruction": instruction}}


def ai_says(title, assignee="Meena", due="2030-01-12"):
    return {"is_assignment": True, "items": [{"title": title, "assignee": assignee, "due_date": due}]}


async def task_count():
    async with async_session() as s:
        return len((await s.execute(select(Task))).scalars().all())


# =====================================================================  CHAT: the user's own test
async def test_resending_same_chat_message_makes_one_task_even_if_ai_words_it_differently(org, sent, llm, monkeypatch):
    msg = "assign a task to Meena to finish the dashboard work within 4 working days"

    llm.assign_json = ai_says("Finish Dashboard Work", due="2030-01-12")
    _script(monkeypatch, AIMessage(content="", tool_calls=[call(msg)]), AIMessage(content="Assigned."))
    await agent.chat(msg, "ceo@example.com")

    llm.assign_json = ai_says("Complete Dashboard Work", due="2030-01-11")      # the AI words it differently
    RecordingFake.seen = []
    monkeypatch.setattr(agent, "get_chat_model", lambda: RecordingFake(responses=[
        AIMessage(content="", tool_calls=[call(msg)]), AIMessage(content="Already done.")]))
    await agent.chat(msg, "ceo@example.com")

    assert await task_count() == 1 and len(sent) == 1
    tool_texts = [c for generation in RecordingFake.seen for t, c in generation if t == "tool"]
    assert any("already" in c.lower() and "no new task" in c.lower() for c in tool_texts)   # the model is told, so it can tell the user


async def test_model_calling_the_tool_twice_in_one_turn_makes_one_task(org, sent, llm, monkeypatch):
    msg = "Ask Meena to send the drawings"
    llm.assign_json = ai_says("Send drawings", due="2030-01-12")
    _script(monkeypatch, AIMessage(content="", tool_calls=[call(msg, "1"), call(msg, "2")]), AIMessage(content="Done."))
    await agent.chat(msg, "ceo@example.com")
    assert await task_count() == 1 and len(sent) == 1


async def test_nothing_created_means_the_same_message_can_be_sent_again(org, sent, llm, monkeypatch):
    """'Ravi' is ambiguous, nothing is created. The leader resends the same words after fixing the people list:
    that must NOT be blocked as a repeat."""
    msg = "Ask Ravi to review the drawings"
    llm.assign_json = ai_says("Review drawings", assignee="Ravi")
    _script(monkeypatch, AIMessage(content="", tool_calls=[call(msg)]), AIMessage(content="Which Ravi?"))
    await agent.chat(msg, "ceo@example.com")
    assert await task_count() == 0

    llm.assign_json = ai_says("Review drawings", assignee="Ravi Kumar")
    _script(monkeypatch, AIMessage(content="", tool_calls=[call(msg)]), AIMessage(content="Assigned."))
    await agent.chat(msg, "ceo@example.com")
    assert await task_count() == 1


async def test_a_different_message_is_never_blocked(org, sent, llm, monkeypatch):
    for msg, title in [("Ask Meena to send drawings", "Send drawings"),
                       ("Ask Meena to book the vendor meeting", "Book vendor meeting")]:
        llm.assign_json = ai_says(title)
        _script(monkeypatch, AIMessage(content="", tool_calls=[call(msg)]), AIMessage(content="ok"))
        await agent.chat(msg, "ceo@example.com")
    assert await task_count() == 2


async def test_two_different_instructions_in_one_message_both_run_and_a_resend_creates_nothing(org, sent, monkeypatch):
    async def smart_ai(system, user):
        if "extract work assignments" in system:
            who, title = ("Meena", "Send drawings") if "Meena" in user else ("Ravi Kumar", "Review report")
            return json.dumps(ai_says(title, assignee=who, due="2030-01-10"))
        return "{}"
    monkeypatch.setattr(llm_tasks, "_invoke", smart_ai)
    msg = "Ask Meena to send drawings and ask Ravi Kumar to review the report"
    two_calls = lambda: AIMessage(content="", tool_calls=[call("Ask Meena to send drawings", "1"),
                                                          call("Ask Ravi Kumar to review the report", "2")])
    _script(monkeypatch, two_calls(), AIMessage(content="Both assigned."))
    await agent.chat(msg, "ceo@example.com")
    assert await task_count() == 2                                   # not mistaken for a repeat of each other

    _script(monkeypatch, two_calls(), AIMessage(content="Both assigned."))
    await agent.chat(msg, "ceo@example.com")                          # the leader sends the whole thing again
    assert await task_count() == 2 and len(sent) == 2


# =====================================================================  EMAIL: the email's own Message-ID
async def test_same_email_again_makes_no_second_task_even_if_ai_words_it_differently(org, sent, llm):
    em = make_email("ceo@example.com", subject="Assign", body="Ask Meena to send the drawings")
    llm.assign_json = ai_says("Send drawings", due="2030-01-12")
    async with async_session() as s:
        await inbox.handle_email(s, em)
    llm.assign_json = ai_says("Dispatch the drawings", due="2030-01-11")      # re-read after a crash: new wording
    async with async_session() as s:
        out = await inbox.handle_email(s, em)
    assert await task_count() == 1
    async with async_session() as s:
        n = len((await s.execute(select(OutboundEmail).where(OutboundEmail.kind == "assignment"))).scalars().all())
    assert n == 1 and out.status == "processed"


async def test_email_whose_ai_read_failed_can_be_retried(org, sent, llm):
    em = make_email("ceo@example.com", subject="Assign", body="Ask Meena to send the drawings")
    llm.fail = True
    async with async_session() as s:
        with pytest.raises(llm_tasks.LLMError):
            await inbox.handle_email(s, em)
    llm.fail = False
    llm.assign_json = ai_says("Send drawings", due="2030-01-12")
    async with async_session() as s:
        await inbox.handle_email(s, em)               # the retry must not think it was already handled
    assert await task_count() == 1


# =====================================================================  the memory itself
async def test_claim_then_repeat_returns_the_saved_answer(org):
    async with async_session() as s:
        first = await idempotency.claim(s, "fp-1", window_minutes=10)
        assert not first.repeat
        await idempotency.finish(s, first.key, "Created task #1")
        again = await idempotency.claim(s, "fp-1", window_minutes=10)
    assert again.repeat and again.summary == "Created task #1"


async def test_release_frees_the_request_to_be_tried_again(org):
    async with async_session() as s:
        first = await idempotency.claim(s, "fp-2", window_minutes=10)
        await idempotency.release(s, first.key)
        assert not (await idempotency.claim(s, "fp-2", window_minutes=10)).repeat


async def test_after_the_window_the_same_request_is_allowed(org, monkeypatch):
    async with async_session() as s:
        assert not (await idempotency.claim(s, "fp-3", window_minutes=10)).repeat
    real = idempotency.utcnow()
    monkeypatch.setattr(idempotency, "utcnow", lambda: real + timedelta(minutes=11))
    async with async_session() as s:
        assert not (await idempotency.claim(s, "fp-3", window_minutes=10)).repeat


async def test_only_one_of_several_simultaneous_claims_wins(org):
    async def one():
        async with async_session() as s:
            return await idempotency.claim(s, "fp-4", window_minutes=10)
    results = await asyncio.gather(one(), one(), one())
    assert sum(1 for r in results if not r.repeat) == 1


async def test_window_zero_switches_the_feature_off(org):
    async with async_session() as s:
        assert not (await idempotency.claim(s, "fp-5", window_minutes=0)).repeat
        assert not (await idempotency.claim(s, "fp-5", window_minutes=0)).repeat


async def test_old_memory_rows_are_cleaned_up(org, monkeypatch):
    async with async_session() as s:
        await idempotency.claim(s, "ancient", window_minutes=10)
    real = idempotency.utcnow()
    monkeypatch.setattr(idempotency, "utcnow", lambda: real + timedelta(days=40))
    async with async_session() as s:
        await idempotency.claim(s, "fresh", window_minutes=10)
        from app.models import RequestLog
        fps = {r.fingerprint for r in (await s.execute(select(RequestLog))).scalars()}
    assert fps == {"fresh"}


# =====================================================================  WORKING DAYS counted by code
FRI = date(2026, 11, 6)       # the Friday from the real test


def test_four_working_days_from_friday_is_thursday():
    assert add_working_days(FRI, 4) == date(2026, 11, 12)         # Mon 9, Tue 10, Wed 11, Thu 12
    assert add_working_days(FRI, 3) == date(2026, 11, 11)
    assert add_working_days(FRI, 1) == date(2026, 11, 9)          # skips the weekend
    assert add_working_days(date(2026, 11, 7), 1) == date(2026, 11, 9)    # from a Saturday
    assert add_working_days(date(2026, 11, 9), 5) == date(2026, 11, 16)   # a full week


async def test_ai_only_reports_the_number_and_code_works_out_the_date(llm):
    llm.assign_json = {"is_assignment": True, "items": [
        {"title": "Dashboard", "assignee": "Meena", "due_in_working_days": 4, "due_date": "2026-11-11"}]}   # AI's date is WRONG
    parsed = await llm_tasks.parse_assignment("within 4 working days", today=FRI)
    assert parsed.items[0].due_date == "2026-11-12"                # code overrides it


async def test_working_days_also_apply_to_meeting_notes(llm):
    llm.notes_json = {"items": [{"title": "T", "assignee": "Meena", "due_in_working_days": 3,
                                 "due_date": "2026-11-10", "evidence": "x"}]}
    parsed = await llm_tasks.parse_notes("x", today=FRI)
    assert parsed.items[0].due_date == "2026-11-11"


@pytest.mark.parametrize("junk", [0, -3, "four", None, 9999, 2.5])
async def test_a_silly_working_day_value_never_breaks_the_parse(llm, junk):
    llm.assign_json = {"is_assignment": True, "items": [
        {"title": "T", "assignee": "Meena", "due_in_working_days": junk, "due_date": "2030-01-10"}]}
    parsed = await llm_tasks.parse_assignment("x", today=FRI)
    assert len(parsed.items) == 1 and parsed.items[0].due_date == "2030-01-10"      # ignored, date untouched
