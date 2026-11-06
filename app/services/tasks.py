"""Core task operations: create+deliver, change status, log history."""
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .. import email_client, email_templates
from ..config import get_settings
from ..models import Employee, OutboundEmail, Task, TaskEvent, TaskStatus
from ..timeutil import fmt_local, utcnow
from . import attachments as attachments_service

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# history
# ----------------------------------------------------------------------
async def log_event(
    session: AsyncSession,
    task_id: int,
    event_type: str,
    *,
    actor_id: Optional[int] = None,
    old_status: Optional[str] = None,
    new_status: Optional[str] = None,
    detail: Optional[str] = None,
) -> None:
    session.add(TaskEvent(
        task_id=task_id, event_type=event_type, actor_id=actor_id,
        old_status=old_status, new_status=new_status, detail=detail,
    ))


# ----------------------------------------------------------------------
# follow-up timing
# ----------------------------------------------------------------------
def followup_gap_hours(due: Optional[datetime], now: datetime) -> int:
    """How long to wait before the next nudge. Closer to the deadline = more often."""
    s = get_settings()
    if due is None:
        return s.gap_far_hours
    remaining = due - now
    if remaining <= timedelta(0):
        return s.gap_overdue_hours
    if remaining <= timedelta(days=2):
        return s.gap_near_hours
    if remaining <= timedelta(days=7):
        return s.gap_mid_hours
    return s.gap_far_hours


def compute_next_followup(due: Optional[datetime], now: datetime) -> datetime:
    return now + timedelta(hours=followup_gap_hours(due, now))


# ----------------------------------------------------------------------
# emails about a task
# ----------------------------------------------------------------------
# Emails that are a CONTINUATION of the conversation with the person who owns the task.
# They are sent as replies to the previous message, so the mail app shows ONE thread:
#   assignment -> reminder -> reminder -> ack ...
# Everything else (escalation, blocker, done notices) goes to OTHER people and starts
# its own conversation.
THREADED_KINDS = {"reminder", "ack", "check", "correction_notice"}
_THREAD_MEMBERS = ("assignment", "reminder", "ack")


async def _thread_parent(session: AsyncSession, task_id: int, to_email: str) -> Optional[str]:
    """Message-ID of the latest email in this task's conversation with this person."""
    res = await session.execute(
        select(OutboundEmail.message_id)
        .where(OutboundEmail.task_id == task_id, OutboundEmail.to_email == to_email,
               OutboundEmail.kind.in_(_THREAD_MEMBERS))
        .order_by(OutboundEmail.sent_at.desc())
        .limit(1)
    )
    row = res.first()
    return row[0] if row else None


async def send_task_email(
    session: AsyncSession, task: Task, *, to: Employee, subject: str, body: str,
    kind: str, cc: Optional[list] = None, html: Optional[str] = None,
    attachments: Optional[list] = None,
) -> str:
    """Send an email about a task and remember its Message-ID for reply matching.

    Reminders and acknowledgements are sent IN THE SAME THREAD as the assignment email."""
    in_reply_to = None
    if kind in THREADED_KINDS:
        in_reply_to = await _thread_parent(session, task.id, to.email)
        if not in_reply_to and subject.startswith("Re: "):
            subject = subject[4:]      # nothing to reply to (assignment never arrived)
    extra = {"attachments": attachments} if attachments else {}     # only passed when there are files
    mid = await email_client.send_email(
        to=to.email, subject=subject, body=body, cc=cc, task_id=task.id, html=html,
        in_reply_to=in_reply_to, **extra,
    )
    session.add(OutboundEmail(message_id=mid, task_id=task.id, to_email=to.email, kind=kind))
    return mid


# Delivery policy: first try right away, then retry after 1, 5 and 15 minutes, then give up.
RETRY_MINUTES = (1, 5, 15)
MAX_DELIVERY_ATTEMPTS = len(RETRY_MINUTES) + 1


async def deliver_assignment(session: AsyncSession, task: Task) -> bool:
    """Send the assignment email and record the outcome on the task.

    Success -> delivery_status "sent". Failure -> stays "waiting" with a later retry time,
    or becomes "failed" once every attempt has been used. The task itself is never lost."""
    try:
        files = await attachments_service.files_for_task(session, task.id)
        text, html = email_templates.assignment(task, task.assigned_by, [(f.filename, f.size) for f in files])
        # Like a real manager, the person who assigned the work is copied on the email,
        # so they can see the conversation. Not when they assigned it to themselves.
        boss = task.assigned_by
        cc = [boss.email] if boss and boss.active and boss.id != task.assignee_id else None
        await send_task_email(
            session, task, to=task.assignee,
            subject=f"[Task #{task.id}] {task.title}",
            body=text, html=html, kind="assignment", cc=cc, attachments=files,
        )
        task.notified_at = utcnow()
        task.delivery_status = "sent"
        task.delivery_attempts = (task.delivery_attempts or 0) + 1
        task.delivery_error = None
        task.delivery_next_at = None
        with_files = f" with {len(files)} attachment(s): {', '.join(f.filename for f in files)}" if files else ""
        await log_event(session, task.id, "delivered",
                        detail=f"assignment emailed to {task.assignee.email}{with_files}"[:300])
        return True
    except Exception as e:
        log.error("could not email task %s: %s", task.id, e)
        task.delivery_attempts = (task.delivery_attempts or 0) + 1
        task.delivery_error = f"{type(e).__name__}: {e}"[:300]
        if task.delivery_attempts >= MAX_DELIVERY_ATTEMPTS:
            task.delivery_status = "failed"
            task.delivery_next_at = None
            detail = f"gave up after {task.delivery_attempts} attempts: {task.delivery_error}"
        else:
            task.delivery_status = "waiting"
            wait = RETRY_MINUTES[task.delivery_attempts - 1]
            task.delivery_next_at = utcnow() + timedelta(minutes=wait)
            detail = f"attempt {task.delivery_attempts} failed, retrying in {wait} min: {task.delivery_error}"
        await log_event(session, task.id, "delivery_failed", detail=detail[:300])
        return False


# ----------------------------------------------------------------------
# idempotency: the same assignment twice must not become two tasks
# ----------------------------------------------------------------------
# "The same assignment" = same assigner + same assignee + same title + same deadline, asked again within
# DUPLICATE_WINDOW_MINUTES. Two layers protect this:
#   1) a look-up before creating (catches repeats, double clicks, a restarted server re-reading an email);
#   2) a UNIQUE rule in the database (catches two requests that arrive at the same instant and both
#      looked before either had saved).
class DuplicateTask(Exception):
    """The database refused a second task with the same dedupe_key."""


def _norm_title(title: str) -> str:
    return " ".join((title or "").split()).casefold()      # ignore case and extra spaces


def make_dedupe_key(assigner_id: Optional[int], assignee_id: int, title: str,
                    due: Optional[datetime], now: datetime, window_minutes: int) -> str:
    """Fingerprint of the request plus the time slot (window-sized) it falls in. Two requests in the same
    slot are less than one window apart, so the look-up in layer 1 would already treat them as repeats."""
    slot = int((now - datetime(1970, 1, 1)).total_seconds() // (window_minutes * 60))
    raw = f"{assigner_id or 0}|{assignee_id}|{_norm_title(title)}|{due.isoformat() if due else 'none'}|{slot}"
    return hashlib.sha256(raw.encode()).hexdigest()[:40]


async def find_recent_duplicate(
    session: AsyncSession, *, assigner_id: Optional[int], assignee_id: int, title: str,
    due_date: Optional[datetime], now: datetime,
) -> Optional[Task]:
    """An identical task created inside the window, if any (oldest first)."""
    cutoff = now - timedelta(minutes=get_settings().duplicate_window_minutes)
    res = await session.execute(
        select(Task).where(
            Task.assignee_id == assignee_id,
            Task.assigned_by_id.is_(None) if assigner_id is None else Task.assigned_by_id == assigner_id,
            Task.due_date.is_(None) if due_date is None else Task.due_date == due_date,
            Task.created_at >= cutoff,
        ).order_by(Task.id)
    )
    wanted = _norm_title(title)
    return next((t for t in res.scalars().unique() if _norm_title(t.title) == wanted), None)


async def create_task_once(
    session: AsyncSession, *, title: str, assignee: Employee,
    assigned_by: Optional[Employee] = None, description: Optional[str] = None,
    due_date: Optional[datetime] = None, attachment_ids: Optional[Sequence[int]] = None,
) -> Tuple[Task, bool]:
    """Create the task unless an identical one was just created. Returns (task, was_duplicate).
    When it is a duplicate the EXISTING task is returned and nothing is saved or emailed."""
    window = get_settings().duplicate_window_minutes
    if window <= 0:                                            # feature switched off
        return await create_task(session, title=title, assignee=assignee, assigned_by=assigned_by,
                                 description=description, due_date=due_date,
                                 attachment_ids=attachment_ids), False
    now = utcnow()
    assignee_id = assignee.id
    assigner_id = assigned_by.id if assigned_by else None

    existing = await find_recent_duplicate(session, assigner_id=assigner_id, assignee_id=assignee_id,
                                           title=title, due_date=due_date, now=now)
    if existing is not None:                                   # layer 1
        return existing, True

    key = make_dedupe_key(assigner_id, assignee_id, title, due_date, now, window)
    try:
        task = await create_task(session, title=title, assignee=assignee, assigned_by=assigned_by,
                                 description=description, due_date=due_date,
                                 attachment_ids=attachment_ids, dedupe_key=key)
        return task, False
    except DuplicateTask:                                      # layer 2: someone else saved it first
        res = await session.execute(select(Task).where(Task.dedupe_key == key))
        existing = res.scalars().unique().first()
        if existing is None:
            raise
        return existing, True


# ----------------------------------------------------------------------
# create / change
# ----------------------------------------------------------------------
async def create_task(
    session: AsyncSession, *, title: str, assignee: Employee,
    assigned_by: Optional[Employee] = None, description: Optional[str] = None,
    due_date: Optional[datetime] = None, attachment_ids: Optional[Sequence[int]] = None,
    dedupe_key: Optional[str] = None,
) -> Task:
    now = utcnow()
    task = Task(
        title=title.strip(), description=description, status=TaskStatus.PENDING.value,
        assignee_id=assignee.id, assigned_by_id=assigned_by.id if assigned_by else None,
        due_date=due_date, next_followup_at=compute_next_followup(due_date, now),
        dedupe_key=dedupe_key,
    )
    if dedupe_key:
        # A SAVEPOINT: if the database refuses the duplicate, only this insert is undone, not the caller's
        # whole transaction (and the people already loaded in the session stay usable).
        try:
            async with session.begin_nested():
                session.add(task)
                await session.flush()
        except IntegrityError as e:
            raise DuplicateTask(dedupe_key) from e
    else:
        session.add(task)
        await session.flush()
    await session.refresh(task, attribute_names=["assignee", "assigned_by"])
    if attachment_ids:
        # linked BEFORE the commit below, so the background sender can never email the task without its files
        await attachments_service.link_to_task(session, task.id, attachment_ids)
    await log_event(
        session, task.id, "created", actor_id=assigned_by.id if assigned_by else None,
        new_status=task.status, detail=f"assigned to {assignee.full_name}",
    )
    if get_settings().async_delivery:
        # Do not make the caller wait for Gmail: the background sender picks this task up.
        task.delivery_status = "waiting"
        task.delivery_next_at = now
        await session.commit()
        from . import delivery   # imported here to avoid a circular import
        delivery.kick()
    else:
        await deliver_assignment(session, task)
        await session.commit()
    return task


async def apply_status(
    session: AsyncSession, task: Task, new_status: str, *,
    actor_id: Optional[int] = None, detail: Optional[str] = None,
    new_due: Optional[datetime] = None, from_reply: bool = True,
) -> bool:
    """Set the status, reset follow-up counters, write history. Returns True if it changed."""
    now = utcnow()
    old = task.status
    changed = old != new_status
    task.status = new_status
    if from_reply:
        task.last_reply_at = now
        task.followup_count = 0
        if task.escalated:
            task.escalated = False
            await log_event(session, task.id, "comment", actor_id=actor_id,
                            detail="assignee replied - escalation cleared")
    if new_due and new_due != task.due_date:
        await log_event(session, task.id, "due_date_changed", actor_id=actor_id,
                        detail=f"{fmt_local(task.due_date)} -> {fmt_local(new_due)} (proposed by assignee)")
        task.due_date = new_due
    if new_status == TaskStatus.DONE.value:
        task.completed_at = now
        task.next_followup_at = None
    else:
        task.completed_at = None
        task.next_followup_at = compute_next_followup(task.due_date, now)
    if changed or detail:
        await log_event(session, task.id, "status_change", actor_id=actor_id,
                        old_status=old, new_status=new_status, detail=detail)
    return changed


async def settle_status_check(session: AsyncSession, task: Task, kind: str, detail: str,
                               actor_id: Optional[int] = None) -> None:
    """Close a status question (or record a correction): kind is status_confirmed, status_corrected
    or status_check_expired. Clears the "unconfirmed" mark."""
    task.status_check = None
    await log_event(session, task.id, kind, actor_id=actor_id, detail=detail[:300])


async def last_status_detail(session: AsyncSession, task_id: int) -> Optional[str]:
    """What the employee said when the current status was set (used for held-back alerts)."""
    res = await session.execute(
        select(TaskEvent.detail).where(TaskEvent.task_id == task_id, TaskEvent.event_type == "status_change")
        .order_by(TaskEvent.id.desc()).limit(1))
    return res.scalar_one_or_none()


async def get_task(session: AsyncSession, task_id: int) -> Optional[Task]:
    return await session.get(Task, task_id)


async def open_tasks_for(session: AsyncSession, emp_id: int) -> list:
    res = await session.execute(
        select(Task).where(Task.assignee_id == emp_id, Task.status != TaskStatus.DONE.value).order_by(Task.id)
    )
    return list(res.scalars().unique())

async def register_reply(session: AsyncSession, task: Task, *, actor_id: Optional[int] = None) -> None:
    """The assignee answered without giving a status: keep the status, but stop the
    silence clock (reset reminder count, clear escalation, schedule the next check)."""
    now = utcnow()
    task.last_reply_at = now
    task.followup_count = 0
    if task.escalated:
        task.escalated = False
        await log_event(session, task.id, "comment", actor_id=actor_id,
                        detail="assignee replied - escalation cleared")
    if task.status != TaskStatus.DONE.value:
        task.next_followup_at = compute_next_followup(task.due_date, now)