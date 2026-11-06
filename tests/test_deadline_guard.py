"""A deadline that has already passed must not silently create an overdue task."""
from datetime import timedelta

from sqlalchemy import select

from app import directory
from app.database import async_session
from app.llm_tasks import AssignmentItem
from app.models import Task
from app.services.assign import assign_from_items
from app.timeutil import to_local, utcnow


async def _ceo(s):
    return await directory.get_by_email(s, "ceo@example.com")


def _day(delta_days):
    return (to_local(utcnow()) + timedelta(days=delta_days)).date().isoformat()


async def test_past_deadline_is_refused_with_a_clear_message(org, sent):
    async with async_session() as s:
        res = await assign_from_items(s, await _ceo(s), [
            AssignmentItem(title="Siemens dashboard", assignee="Meena", due_date=_day(-1))])
        assert res.created == []
        assert "already passed" in res.summary() and "no task was created" in res.summary()
    assert sent == []                                              # nobody was emailed
    async with async_session() as s:
        assert (await s.execute(select(Task))).scalars().first() is None


async def test_future_and_missing_deadlines_still_work(org, sent):
    async with async_session() as s:
        res = await assign_from_items(s, await _ceo(s), [
            AssignmentItem(title="Future", assignee="Meena", due_date=_day(3)),
            AssignmentItem(title="No deadline", assignee="Meena", due_date=None)])
        assert len(res.created) == 2 and not res.problems


async def test_one_bad_deadline_does_not_block_the_other_items(org, sent):
    async with async_session() as s:
        res = await assign_from_items(s, await _ceo(s), [
            AssignmentItem(title="Old", assignee="Meena", due_date=_day(-2)),
            AssignmentItem(title="Fine", assignee="Ravi Kumar", due_date=_day(5))])
        assert [t.title for t in res.created] == ["Fine"] and len(res.problems) == 1
