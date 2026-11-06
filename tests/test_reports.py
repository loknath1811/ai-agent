from datetime import timedelta

from app import directory
from app.database import async_session
from app.llm_tasks import AssignmentItem
from app.services import reports
from app.services import tasks as ts
from app.services.assign import assign_from_items
from app.timeutil import utcnow


async def _mk(title, who, days, status=None):
    async with async_session() as s:
        ceo = await directory.get_by_email(s, "ceo@example.com")
        if days < 0:
            # a leader's instruction with a past deadline is refused (see test_deadline_guard.py),
            # so build the already-overdue task directly to test the overdue numbers
            person = (await directory.resolve(s, who)).employee
            t = await ts.create_task(s, title=title, assignee=person, assigned_by=ceo,
                                     due_date=utcnow() + timedelta(days=days))
        else:
            due = (utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")
            t = (await assign_from_items(s, ceo, [AssignmentItem(title=title, assignee=who, due_date=due)])).created[0]
        if status:
            await ts.apply_status(s, t, status, detail="test")
            await s.commit()
        return t.id


async def test_rollups_person_team_division_company(org, sent):
    await _mk("a", "Ravi Kumar", 5, "done")
    await _mk("b", "Meena", 5, "on-track")
    await _mk("c", "Ravi Kumar", -3, "blocked")           # overdue + blocked
    await _mk("d", "Ravi Sharma", 5)                      # pending, Design

    async with async_session() as s:
        co = await reports.company_report(s)
        assert co["summary"]["total"] == 4 and co["summary"]["overdue"] == 1
        assert co["summary"]["by_status"]["done"] == 1 and co["summary"]["completion_pct"] == 25
        names = {d["name"]: d["summary"]["total"] for d in co["divisions"]}
        assert names == {"Engineering": 1, "Quality": 3}
        assert co["attention"][0]["title"] == "c"         # blocked+overdue floats to the top

        qa = await directory.find_team(s, "QA")
        tr = await reports.team_report(s, qa.id)
        assert tr["summary"]["total"] == 3
        member = {m["name"]: m["summary"]["total"] for m in tr["members"]}
        assert member["Ravi Kumar"] == 2 and member["Meena Iyer"] == 1

        ravi = await directory.get_by_email(s, "ravi.kumar@example.com")
        pr = await reports.person_report(s, ravi.id)
        assert pr["summary"]["total"] == 2 and len(pr["tasks"]) == 2

        div = await directory.find_division(s, "Quality")
        dr = await reports.division_report(s, div.id)
        assert dr["summary"]["total"] == 3 and [t["name"] for t in dr["teams"]] == ["QA"]


def test_describe_text():
    s = reports.summarize([])
    assert "0 tasks" in reports.describe("QA", s)
