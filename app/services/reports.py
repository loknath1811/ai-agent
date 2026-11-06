"""Status roll-ups: person -> team -> division -> company."""
from collections import defaultdict
from datetime import datetime
from typing import Dict, Iterable, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import Division, Employee, Task, TaskStatus, Team
from ..timeutil import utcnow
from . import risk

STATUSES = [s.value for s in TaskStatus]


def is_overdue(t: Task, now: datetime) -> bool:
    return t.status != TaskStatus.DONE.value and t.due_date is not None and t.due_date < now


def summarize(tasks: Iterable[Task], now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    tasks = list(tasks)
    by_status = {s: 0 for s in STATUSES}
    for t in tasks:
        by_status[t.status] = by_status.get(t.status, 0) + 1
    total = len(tasks)
    done = by_status[TaskStatus.DONE.value]
    return {
        "total": total,
        "open": total - done,
        "by_status": by_status,
        "overdue": sum(1 for t in tasks if is_overdue(t, now)),
        "escalated": sum(1 for t in tasks if t.escalated and t.status != TaskStatus.DONE.value),
        "completion_pct": round(100 * done / total) if total else 0,
    }


def task_row(t: Task, now: Optional[datetime] = None) -> dict:
    now = now or utcnow()
    return {
        "id": t.id, "title": t.title, "description": t.description, "status": t.status,
        "delivery_status": t.delivery_status, "delivery_error": t.delivery_error,
        "status_check": t.status_check,
        "assignee_id": t.assignee_id, "assignee_name": t.assignee.full_name,
        "assignee_email": t.assignee.email,
        "team": t.assignee.team.name if t.assignee.team else None,
        "assigned_by": t.assigned_by.full_name if t.assigned_by else None,
        "due_date": t.due_date, "overdue": is_overdue(t, now), "escalated": t.escalated,
        "followup_count": t.followup_count, "last_reply_at": t.last_reply_at,
        "created_at": t.created_at, "updated_at": t.updated_at,
    }


def attention(tasks: Iterable[Task], now: datetime, limit: int = 30) -> List[dict]:
    """Things a leader should look at: blocked, escalated, overdue, delayed."""
    def score(t: Task) -> int:
        return (4 if t.escalated else 0) + (3 if t.status == "blocked" else 0) \
            + (2 if is_overdue(t, now) else 0) + (1 if t.status == "delayed" else 0)
    flagged = [t for t in tasks if t.status != "done" and score(t) > 0]
    flagged.sort(key=lambda t: (-score(t), t.due_date or datetime.max))
    return [task_row(t, now) for t in flagged[:limit]]


def at_risk(tasks: Iterable[Task], now: datetime, limit: int = 15) -> List[dict]:
    """Open tasks heading for trouble, soonest deadline first, each with the reasons it was flagged."""
    flagged = [(t, risk.risk_reasons(t, now)) for t in tasks]
    flagged = [(t, r) for t, r in flagged if r]
    flagged.sort(key=lambda tr: tr[0].due_date or datetime.max)
    return [{**task_row(t, now), "reasons": r} for t, r in flagged[:limit]]


async def _load(session: AsyncSession):
    tasks = list((await session.execute(select(Task))).scalars().unique())
    emps = list((await session.execute(select(Employee))).scalars().unique())
    teams = list((await session.execute(select(Team))).scalars().unique())
    divs = list((await session.execute(select(Division))).scalars().unique())
    return tasks, emps, teams, divs


def _by_team(tasks: List[Task]) -> Dict[Optional[int], List[Task]]:
    out: Dict[Optional[int], List[Task]] = defaultdict(list)
    for t in tasks:
        out[t.assignee.team_id].append(t)
    return out


async def person_report(session: AsyncSession, emp_id: int) -> Optional[dict]:
    now = utcnow()
    emp = await session.get(Employee, emp_id)
    if not emp:
        return None
    tasks = list((await session.execute(select(Task).where(Task.assignee_id == emp_id).order_by(Task.id.desc()))).scalars().unique())
    return {
        "scope": "person",
        "id": emp.id, "name": emp.full_name, "email": emp.email,
        "team": emp.team.name if emp.team else None,
        "summary": summarize(tasks, now),
        "tasks": [task_row(t, now) for t in tasks],
    }


async def team_report(session: AsyncSession, team_id: int) -> Optional[dict]:
    now = utcnow()
    tasks, emps, teams, _ = await _load(session)
    team = next((t for t in teams if t.id == team_id), None)
    if not team:
        return None
    team_tasks = [t for t in tasks if t.assignee.team_id == team_id]
    members = []
    for e in sorted((e for e in emps if e.team_id == team_id and e.active), key=lambda e: e.full_name):
        mine = [t for t in team_tasks if t.assignee_id == e.id]
        members.append({"id": e.id, "name": e.full_name, "email": e.email, "summary": summarize(mine, now)})
    return {
        "scope": "team", "id": team.id, "name": team.name,
        "division": team.division.name if team.division else None,
        "summary": summarize(team_tasks, now),
        "members": members,
        "attention": attention(team_tasks, now),
        "at_risk": at_risk(team_tasks, now),
    }


async def division_report(session: AsyncSession, division_id: int) -> Optional[dict]:
    now = utcnow()
    tasks, _, teams, divs = await _load(session)
    div = next((d for d in divs if d.id == division_id), None)
    if not div:
        return None
    div_teams = [t for t in teams if t.division_id == division_id]
    by_team = _by_team(tasks)
    div_tasks = [t for tm in div_teams for t in by_team.get(tm.id, [])]
    return {
        "scope": "division", "id": div.id, "name": div.name,
        "summary": summarize(div_tasks, now),
        "teams": [{"id": tm.id, "name": tm.name, "summary": summarize(by_team.get(tm.id, []), now)} for tm in sorted(div_teams, key=lambda x: x.name)],
        "attention": attention(div_tasks, now),
        "at_risk": at_risk(div_tasks, now),
    }


async def company_report(session: AsyncSession) -> dict:
    now = utcnow()
    tasks, emps, teams, divs = await _load(session)
    by_team = _by_team(tasks)

    def team_block(tm: Team) -> dict:
        return {"id": tm.id, "name": tm.name, "summary": summarize(by_team.get(tm.id, []), now)}

    divisions = []
    for d in sorted(divs, key=lambda x: x.name):
        d_teams = [tm for tm in teams if tm.division_id == d.id]
        d_tasks = [t for tm in d_teams for t in by_team.get(tm.id, [])]
        divisions.append({"id": d.id, "name": d.name, "summary": summarize(d_tasks, now),
                          "teams": [team_block(tm) for tm in sorted(d_teams, key=lambda x: x.name)]})
    loose_teams = [tm for tm in teams if tm.division_id is None]
    no_team_tasks = by_team.get(None, [])
    if loose_teams or no_team_tasks:
        l_tasks = [t for tm in loose_teams for t in by_team.get(tm.id, [])] + no_team_tasks
        divisions.append({"id": None, "name": "(No division)", "summary": summarize(l_tasks, now),
                          "teams": [team_block(tm) for tm in loose_teams]})
    return {
        "scope": "company",
        "headcount": sum(1 for e in emps if e.active),
        "summary": summarize(tasks, now),
        "divisions": divisions,
        "attention": attention(tasks, now),
        "at_risk": at_risk(tasks, now),
    }


def describe(name: str, s: dict) -> str:
    b = s["by_status"]
    return (
        f"{name}: {s['total']} tasks - {b['done']} done ({s['completion_pct']}%), "
        f"{b['on-track']} on-track, {b['pending']} pending, {b['delayed']} delayed, "
        f"{b['blocked']} blocked; {s['overdue']} overdue, {s['escalated']} escalated."
    )
