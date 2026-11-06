import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import directory, email_templates
from .database import get_session
from .llm_tasks import AssignmentItem
from .models import Employee, Task, TaskEvent, TaskStatus
from .schemas import CorrectIn, DeliveryRead, EventRead, TaskCreate, TaskRead, TaskUpdate
from .security import require_api_key
from .services import tasks as task_service
from .services.assign import assign_from_items
from .services.reports import task_row
from .timeutil import parse_due, utcnow

log = logging.getLogger(__name__)
router = APIRouter(prefix="/tasks", tags=["tasks"], dependencies=[Depends(require_api_key)])


@router.get("/delivery", response_model=List[DeliveryRead])
async def delivery_status(
    ids: str = Query(..., description="comma-separated task ids, e.g. 12,13"),
    session: AsyncSession = Depends(get_session),
):
    """Has the assignment email gone out? (waiting / sent / failed) - the chat polls this."""
    try:
        wanted = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(422, "ids must be whole numbers separated by commas")
    if not wanted or len(wanted) > 50:
        raise HTTPException(422, "give between 1 and 50 task ids")
    res = await session.execute(select(Task).where(Task.id.in_(wanted)).order_by(Task.id))
    return list(res.scalars().unique())


@router.post("", response_model=List[TaskRead], status_code=201)
async def assign(body: TaskCreate, response: Response, session: AsyncSession = Depends(get_session)):
    """Assign a task directly (no AI). `assignee` can be an email, a name or a team."""
    assigner = await directory.get_by_email(session, body.assigned_by_email) if body.assigned_by_email else None
    if body.assigned_by_email and not assigner:
        raise HTTPException(404, "assigned_by_email is not an employee")
    if body.due_date and not parse_due(body.due_date):
        raise HTTPException(422, "due_date must be YYYY-MM-DD or YYYY-MM-DDTHH:MM")
    item = AssignmentItem(title=body.title, assignee=body.assignee,
                          description=body.description, due_date=body.due_date)
    result = await assign_from_items(session, assigner, [item])
    if not result.created and not result.duplicates:
        raise HTTPException(422, "; ".join(result.problems))
    if not result.created:
        response.status_code = 200      # an identical task already exists: same answer as the first time, not an error
    now = utcnow()
    return [task_row(t, now) for t in (result.created or result.duplicates)]


@router.get("", response_model=List[TaskRead])
async def list_tasks(
    status: Optional[TaskStatus] = None,
    assignee_id: Optional[int] = None,
    team_id: Optional[int] = None,
    open_only: bool = False,
    session: AsyncSession = Depends(get_session),
):
    q = select(Task).join(Employee, Task.assignee_id == Employee.id).order_by(Task.id.desc())
    if status:
        q = q.where(Task.status == status.value)
    if assignee_id:
        q = q.where(Task.assignee_id == assignee_id)
    if team_id:
        q = q.where(Employee.team_id == team_id)
    if open_only:
        q = q.where(Task.status != TaskStatus.DONE.value)
    now = utcnow()
    return [task_row(t, now) for t in (await session.execute(q)).scalars().unique()]


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(task_id: int, session: AsyncSession = Depends(get_session)):
    t = await task_service.get_task(session, task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    return task_row(t)


@router.get("/{task_id}/events", response_model=List[EventRead])
async def task_history(task_id: int, session: AsyncSession = Depends(get_session)):
    res = await session.execute(select(TaskEvent).where(TaskEvent.task_id == task_id).order_by(TaskEvent.id))
    return list(res.scalars())


@router.patch("/{task_id}", response_model=TaskRead)
async def manual_update(task_id: int, body: TaskUpdate, session: AsyncSession = Depends(get_session)):
    t = await task_service.get_task(session, task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    if body.status:
        await task_service.apply_status(session, t, body.status, detail="manual update via API", from_reply=False)
    if body.due_date:
        due = parse_due(body.due_date)
        if not due:
            raise HTTPException(422, "due_date must be YYYY-MM-DD or YYYY-MM-DDTHH:MM")
        t.due_date = due
        await task_service.log_event(session, t.id, "due_date_changed", detail="manual update via API")
    await session.commit()
    return task_row(t)


@router.post("/{task_id}/correct", response_model=TaskRead)
async def correct_status(task_id: int, body: CorrectIn, session: AsyncSession = Depends(get_session)):
    """A leader fixes a status the agent got wrong (or confirms one it was unsure about).

    Allowed for the person who assigned the task and for leaders above the assignee. Everything is recorded
    in the task history, and the assignee is told, so a status never changes behind someone's back."""
    t = await task_service.get_task(session, task_id)
    if not t:
        raise HTTPException(404, "Task not found")
    actor = await directory.get_by_email(session, body.as_email) if body.as_email else None
    if actor is None or not actor.active or not actor.is_leader:
        raise HTTPException(403, "Only leaders can correct a status. Pick a leader in the 'chatting as' box.")
    above = {e.id for e in await directory.escalation_chain(session, t.assignee)}
    if actor.id != t.assigned_by_id and actor.id not in above:
        raise HTTPException(403, "Only the person who assigned this task, or a leader above the assignee, can correct its status.")

    before, after = t.status, body.status
    if after == before:
        if t.status_check != "unconfirmed":
            raise HTTPException(409, f"The status is already '{before}'.")
        await task_service.settle_status_check(session, t, "status_confirmed",
                                               f"confirmed as '{before}' by {actor.full_name}", actor.id)
    else:
        await task_service.apply_status(session, t, after, actor_id=actor.id, from_reply=False,
                                        detail=f"corrected by {actor.full_name}")
        await task_service.settle_status_check(session, t, "status_corrected",
                                               f"{before} -> {after} by {actor.full_name}", actor.id)
        try:
            text, html = email_templates.status_correction_notice(t.assignee, t, before, after, actor.full_name)
            await task_service.send_task_email(
                session, t, to=t.assignee, subject=f"Re: [Task #{t.id}] {t.title}",
                body=text, html=html, kind="correction_notice")
        except Exception as e:                       # the correction stands even if the notice cannot be sent
            log.error("could not notify assignee about the correction of task %s: %s", t.id, e)
    await session.commit()
    return task_row(t)
