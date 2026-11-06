"""Escalation and notifications to the people above an assignee."""
import logging
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from .. import directory, email_templates
from ..models import Employee, Task
from ..timeutil import fmt_local, utcnow
from . import tasks as task_service

log = logging.getLogger(__name__)


async def recipients_for(session: AsyncSession, task: Task) -> List[Employee]:
    """Assigner first, then manager chain and team leader. No duplicates, never the assignee."""
    people: List[Employee] = []
    if task.assigned_by and task.assigned_by.active:
        people.append(task.assigned_by)
    people += await directory.escalation_chain(session, task.assignee)
    seen, out = {task.assignee_id}, []
    for p in people:
        if p.id not in seen:
            seen.add(p.id)
            out.append(p)
    return out


async def _notify(session: AsyncSession, task: Task, subject: str, build, kind: str) -> int:
    """Email everyone above the assignee. `build(person)` returns (plain_text, html)."""
    sent = 0
    for person in await recipients_for(session, task):
        try:
            text, html = build(person)
            await task_service.send_task_email(
                session, task, to=person, subject=subject, body=text, html=html, kind=kind)
            sent += 1
        except Exception as e:
            log.error("could not notify %s about task %s: %s", person.email, task.id, e)
    return sent


async def escalate(session: AsyncSession, task: Task, reason: str) -> None:
    """Flag the task and tell the assigner + management chain."""
    task.escalated = True
    task.escalated_at = utcnow()
    task.next_followup_at = None  # stop nagging the assignee; a human takes over
    sent = await _notify(
        session, task,
        subject=f"[Task #{task.id}] ESCALATION: {task.title}",
        build=lambda person: email_templates.escalation(person, task, reason),
        kind="escalation",
    )
    await task_service.log_event(
        session, task.id, "escalated",
        detail=f"{reason} (notified {sent} {'person' if sent == 1 else 'people'})",
    )


async def notify_blocker(session: AsyncSession, task: Task, blocker: Optional[str]) -> None:
    what = blocker or "the assignee reports they are blocked"
    sent = await _notify(
        session, task,
        subject=f"[Task #{task.id}] BLOCKED: {task.title}",
        build=lambda person: email_templates.blocker(person, task, what),
        kind="blocker",
    )
    await task_service.log_event(session, task.id, "blocker_reported", detail=f"{what} (notified {sent})")


async def notify_done(session: AsyncSession, task: Task, summary: Optional[str]) -> None:
    """Short FYI to whoever assigned the task."""
    if not task.assigned_by or not task.assigned_by.active or task.assigned_by_id == task.assignee_id:
        return
    try:
        text, html = email_templates.done_notice(task.assigned_by, task, summary)
        await task_service.send_task_email(
            session, task, to=task.assigned_by,
            subject=f"[Task #{task.id}] DONE: {task.title}", body=text, html=html, kind="done_notice",
        )
    except Exception as e:
        log.error("could not send done notice for task %s: %s", task.id, e)
