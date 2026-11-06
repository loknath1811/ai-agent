"""Organization: divisions, teams, people. Every change is validated and written to a change log."""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_session
from .models import Division, Employee, OrgChange, Task, TaskStatus, Team
from .schemas import (DivisionIn, DivisionRead, EmployeeIn, EmployeeRead, EmployeeUpdate,
                      ImportIn, NameUpdate, TeamIn, TeamRead, TeamUpdate)
from .security import require_api_key
from .services import org_import

router = APIRouter(prefix="/org", tags=["organization"], dependencies=[Depends(require_api_key)])


def _log(session: AsyncSession, action: str, target: str, detail: str = "") -> None:
    session.add(OrgChange(action=action, target=target, detail=detail or None))


async def _commit(session: AsyncSession, what: str) -> None:
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(409, f"{what} already exists or references something that does not exist")


async def _name_taken(session: AsyncSession, model, name: str, exclude_id: Optional[int] = None) -> bool:
    q = select(model.id).where(func.lower(model.name) == name.lower())
    if exclude_id is not None:
        q = q.where(model.id != exclude_id)
    return (await session.execute(q)).first() is not None


def _need_name(name: Optional[str], what: str) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(422, f"{what} name cannot be empty")
    return name


# ---------------- divisions ----------------
@router.get("/divisions", response_model=List[DivisionRead])
async def list_divisions(session: AsyncSession = Depends(get_session)):
    return list((await session.execute(select(Division).order_by(Division.name))).scalars().unique())


@router.post("/divisions", response_model=DivisionRead, status_code=201)
async def create_division(body: DivisionIn, session: AsyncSession = Depends(get_session)):
    name = _need_name(body.name, "Division")
    if await _name_taken(session, Division, name):
        raise HTTPException(409, f"A division called '{name}' already exists")
    d = Division(name=name)
    session.add(d)
    _log(session, "division_added", name)
    await _commit(session, "Division")
    return d


@router.patch("/divisions/{div_id}", response_model=DivisionRead)
async def update_division(div_id: int, body: NameUpdate, session: AsyncSession = Depends(get_session)):
    d = await session.get(Division, div_id)
    if not d:
        raise HTTPException(404, "Division not found")
    name = _need_name(body.name, "Division")
    if await _name_taken(session, Division, name, div_id):
        raise HTTPException(409, f"A division called '{name}' already exists")
    _log(session, "division_renamed", name, f"was '{d.name}'")
    d.name = name
    await _commit(session, "Division")
    return d


# ---------------- teams ----------------
@router.get("/teams", response_model=List[TeamRead])
async def list_teams(session: AsyncSession = Depends(get_session)):
    return list((await session.execute(select(Team).order_by(Team.name))).scalars().unique())


@router.post("/teams", response_model=TeamRead, status_code=201)
async def create_team(body: TeamIn, session: AsyncSession = Depends(get_session)):
    name = _need_name(body.name, "Team")
    if await _name_taken(session, Team, name):
        raise HTTPException(409, f"A team called '{name}' already exists")
    if body.division_id is not None and not await session.get(Division, body.division_id):
        raise HTTPException(422, "That division does not exist")
    t = Team(name=name, division_id=body.division_id)
    session.add(t)
    _log(session, "team_added", name)
    await _commit(session, "Team")
    return t


@router.patch("/teams/{team_id}", response_model=TeamRead)
async def update_team(team_id: int, body: TeamUpdate, session: AsyncSession = Depends(get_session)):
    t = await session.get(Team, team_id)
    if not t:
        raise HTTPException(404, "Team not found")
    data = body.model_dump(exclude_unset=True)
    if "name" in data:
        name = _need_name(data["name"], "Team")
        if await _name_taken(session, Team, name, team_id):
            raise HTTPException(409, f"A team called '{name}' already exists")
        _log(session, "team_renamed", name, f"was '{t.name}'")
        t.name = name
    if "division_id" in data:
        if data["division_id"] is not None and not await session.get(Division, data["division_id"]):
            raise HTTPException(422, "That division does not exist")
        t.division_id = data["division_id"]
        _log(session, "team_moved", t.name, f"division_id -> {data['division_id']}")
    await _commit(session, "Team")
    await session.refresh(t)
    return t


# ---------------- employees ----------------
async def _check_links(session: AsyncSession, *, team_id: Optional[int], manager_id: Optional[int],
                       emp_id: Optional[int] = None) -> None:
    if team_id is not None and not await session.get(Team, team_id):
        raise HTTPException(422, "That team does not exist")
    if manager_id is None:
        return
    mgr = await session.get(Employee, manager_id)
    if not mgr:
        raise HTTPException(422, "That manager does not exist")
    if not mgr.active:
        raise HTTPException(422, f"{mgr.full_name} is inactive and cannot be a manager")
    if emp_id is not None:
        seen, cur = {emp_id}, mgr
        while cur is not None:
            if cur.id in seen:
                raise HTTPException(422, "That would make a management loop (A manages B manages A)")
            seen.add(cur.id)
            cur = await session.get(Employee, cur.manager_id) if cur.manager_id else None


async def _clean_email(session: AsyncSession, raw: str, exclude_id: Optional[int] = None) -> str:
    problem = org_import.email_problem(raw)
    if problem:
        raise HTTPException(422, problem[0].upper() + problem[1:])
    email = raw.strip().lower()
    q = select(Employee).where(Employee.email == email)
    if exclude_id is not None:
        q = q.where(Employee.id != exclude_id)
    other = (await session.execute(q)).scalars().first()
    if other:
        extra = " It is inactive - edit that person and reactivate them instead." if not other.active else ""
        raise HTTPException(409, f"{email} already belongs to {other.full_name}.{extra}")
    return email


@router.get("/employees", response_model=List[EmployeeRead])
async def list_employees(session: AsyncSession = Depends(get_session)):
    return list((await session.execute(select(Employee).order_by(Employee.full_name))).scalars().unique())


@router.post("/employees", response_model=EmployeeRead, status_code=201)
async def create_employee(body: EmployeeIn, session: AsyncSession = Depends(get_session)):
    name = _need_name(body.full_name, "Person")
    email = await _clean_email(session, body.email)
    await _check_links(session, team_id=body.team_id, manager_id=body.manager_id)
    emp = Employee(full_name=name, email=email, title=(body.title or "").strip() or None,
                   team_id=body.team_id, manager_id=body.manager_id, is_leader=body.is_leader)
    session.add(emp)
    _log(session, "employee_added", f"{name} <{email}>")
    await _commit(session, "Employee")
    return emp


@router.get("/employees/{emp_id}/impact")
async def employee_impact(emp_id: int, session: AsyncSession = Depends(get_session)):
    """What deactivating this person would affect (shown before the admin confirms)."""
    if not await session.get(Employee, emp_id):
        raise HTTPException(404, "Employee not found")
    open_tasks = (await session.execute(
        select(func.count(Task.id)).where(Task.assignee_id == emp_id, Task.status != TaskStatus.DONE.value)
    )).scalar_one()
    reports = (await session.execute(
        select(func.count(Employee.id)).where(Employee.manager_id == emp_id, Employee.active.is_(True))
    )).scalar_one()
    return {"open_tasks": open_tasks, "direct_reports": reports}


@router.patch("/employees/{emp_id}", response_model=EmployeeRead)
async def update_employee(emp_id: int, body: EmployeeUpdate, session: AsyncSession = Depends(get_session)):
    emp = await session.get(Employee, emp_id)
    if not emp:
        raise HTTPException(404, "Employee not found")
    data = body.model_dump(exclude_unset=True)
    if "full_name" in data:
        data["full_name"] = _need_name(data["full_name"], "Person")
    if "email" in data:
        data["email"] = await _clean_email(session, data["email"] or "", exclude_id=emp_id)
    if "title" in data:
        data["title"] = (data["title"] or "").strip() or None
    if "team_id" in data or "manager_id" in data:
        await _check_links(session, team_id=data.get("team_id"), manager_id=data.get("manager_id"),
                           emp_id=emp_id)
    was_active = emp.active
    changed = [k for k, v in data.items() if getattr(emp, k) != v]
    for k, v in data.items():
        setattr(emp, k, v)
    if changed:
        action = "employee_changed"
        detail = "changed: " + ", ".join(changed)
        if was_active and emp.active is False:
            action, detail = "employee_deactivated", "no longer receives or is chased for tasks"
        elif not was_active and emp.active is True:
            action, detail = "employee_reactivated", ""
        _log(session, action, f"{emp.full_name} <{emp.email}>", detail)
    await _commit(session, "Employee")
    return emp


# ---------------- bulk import (preview first, then apply) ----------------
@router.post("/import/preview")
async def import_preview(body: ImportIn, session: AsyncSession = Depends(get_session)):
    try:
        return await org_import.process(session, body.csv, apply=False)
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.post("/import/apply")
async def import_apply(body: ImportIn, session: AsyncSession = Depends(get_session)):
    try:
        result = await org_import.process(session, body.csv, apply=True)
    except ValueError as e:
        raise HTTPException(422, str(e))
    if not result["applied"]:
        raise HTTPException(422, "Nothing was saved: fix the errors in the file and preview again.")
    return result


# ---------------- everything the People page needs, in ONE request ----------------
@router.get("/overview")
async def overview(session: AsyncSession = Depends(get_session)):
    """People, teams, divisions and recent changes together: one request and one database
    connection instead of four (much faster when the database is on another network)."""
    emps = list((await session.execute(select(Employee).order_by(Employee.full_name))).scalars().unique())
    teams = list((await session.execute(select(Team).order_by(Team.name))).scalars().unique())
    divs = list((await session.execute(select(Division).order_by(Division.name))).scalars().unique())
    changes = (await session.execute(select(OrgChange).order_by(OrgChange.id.desc()).limit(10))).scalars().all()
    return {
        "employees": [EmployeeRead.model_validate(e).model_dump() for e in emps],
        "teams": [TeamRead.model_validate(t).model_dump() for t in teams],
        "divisions": [DivisionRead.model_validate(d).model_dump() for d in divs],
        "changes": [{"id": c.id, "created_at": c.created_at.isoformat(), "action": c.action,
                     "target": c.target, "detail": c.detail} for c in changes],
    }


# ---------------- change log ----------------
@router.get("/changes")
async def recent_changes(limit: int = 30, session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(OrgChange).order_by(OrgChange.id.desc()).limit(max(1, min(limit, 200))))).scalars().all()
    return [{"id": c.id, "created_at": c.created_at.isoformat(), "action": c.action,
             "target": c.target, "detail": c.detail} for c in rows]
