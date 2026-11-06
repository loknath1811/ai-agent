from sqlalchemy import select

from app import directory
from app.database import async_session
from app.llm_tasks import AssignmentItem
from app.models import OutboundEmail, Task, TaskEvent
from app.services.assign import assign_from_items


async def _ceo(s):
    return await directory.get_by_email(s, "ceo@example.com")


async def test_person_assignment_sends_email_and_logs(org, sent):
    async with async_session() as s:
        res = await assign_from_items(s, await _ceo(s), [
            AssignmentItem(title="NCR review", assignee="Ravi Kumar", due_date="2030-01-10")])
        assert len(res.created) == 1 and not res.problems
    assert len(sent) == 1
    assert sent[0]["to"] == "ravi.kumar@example.com"
    assert f"[Task #{res.created[0].id}]" in sent[0]["subject"]
    async with async_session() as s:
        t = (await s.execute(select(Task))).scalars().first()
        assert t.status == "pending" and t.notified_at is not None and t.next_followup_at is not None
        kinds = [e.event_type for e in (await s.execute(select(TaskEvent))).scalars()]
        assert kinds == ["created", "delivered"]
        assert (await s.execute(select(OutboundEmail))).scalars().first().kind == "assignment"


async def test_team_goes_to_lead_by_default_and_fans_out_on_request(org, sent):
    async with async_session() as s:
        res = await assign_from_items(s, await _ceo(s), [AssignmentItem(title="Audit", assignee="QA team")])
        assert [t.assignee.email for t in res.created] == ["anita@example.com"]
        res2 = await assign_from_items(s, await _ceo(s), [
            AssignmentItem(title="Training", assignee="QA team", fan_out=True)])
        assert sorted(t.assignee.email for t in res2.created) == [
            "anita@example.com", "meena@example.com", "ravi.kumar@example.com"]


async def test_ambiguous_and_unknown_names_are_never_guessed(org, sent):
    async with async_session() as s:
        res = await assign_from_items(s, await _ceo(s), [
            AssignmentItem(title="X", assignee="Ravi"),
            AssignmentItem(title="Y", assignee="Zebulon")])
        assert res.created == [] and len(res.problems) == 2
        assert "Which one" in res.problems[0]
    assert sent == []


async def test_failed_delivery_keeps_task_for_retry(org, monkeypatch):
    from app import email_client

    async def boom(*a, **k):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(email_client, "send_email", boom)
    async with async_session() as s:
        res = await assign_from_items(s, await _ceo(s), [AssignmentItem(title="T", assignee="Meena")])
        assert res.created[0].notified_at is None
        kinds = [e.event_type for e in (await s.execute(select(TaskEvent))).scalars()]
        assert "delivery_failed" in kinds
