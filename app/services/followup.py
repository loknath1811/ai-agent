"""The proactive part: nudge people who have not replied, escalate silence and overdue work."""
import logging
import re
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import email_templates
from ..config import get_settings
from ..models import Employee, Task, TaskStatus
from ..timeutil import fmt_local, is_work_time, utcnow
from . import delivery, escalation
from . import tasks as task_service

log = logging.getLogger(__name__)


async def _send_reminder(session: AsyncSession, task: Task, now: datetime) -> None:
    text, html = email_templates.reminder(task)
    await task_service.send_task_email(
        session, task, to=task.assignee,
        subject=f"Re: [Task #{task.id}] {task.title}",
        body=text, html=html, kind="reminder",
    )
    task.followup_count += 1
    task.last_followup_at = now
    task.next_followup_at = task_service.compute_next_followup(task.due_date, now)
    await task_service.log_event(
        session, task.id, "reminder_sent",
        detail=f"reminder #{task.followup_count}; next check {fmt_local(task.next_followup_at)}",
    )


# ----------------------------------------------------------------------
# manual follow-up: a leader asks in the chat ("follow up with Ravi about the Siemens dashboard")
# ----------------------------------------------------------------------
_HINT_STOP = {
    "a", "an", "the", "to", "of", "for", "on", "in", "at", "about", "regarding", "with",
    "and", "or", "his", "her", "their", "this", "that", "one", "task", "tasks", "work",
    "status", "follow", "followup", "up", "update", "remind", "reminder", "send",
    "email", "mail", "please",
}


def _words(text: str) -> list:
    return [w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in _HINT_STOP]


def _same_word(a: str, b: str) -> bool:
    if a == b:
        return True
    return len(a) >= 4 and len(b) >= 4 and (a.startswith(b) or b.startswith(a))


async def open_tasks_for(session: AsyncSession, employee_id: int) -> list:
    """Every task of this person that is not done yet (oldest first)."""
    res = await session.execute(
        select(Task)
        .where(Task.assignee_id == employee_id, Task.status != TaskStatus.DONE.value)
        .order_by(Task.created_at)
    )
    return list(res.scalars().unique())


def match_tasks(tasks: list, hint: str) -> list:
    """Tasks whose title/details best match the words in `hint` (only the best score)."""
    hint_words = _words(hint)
    if not hint_words:
        return []
    scored = []
    for t in tasks:
        words = _words(f"{t.title} {t.description or ''}")
        score = sum(1 for h in hint_words if any(_same_word(h, w) for w in words))
        if score:
            scored.append((score, t))
    if not scored:
        return []
    best = max(score for score, _ in scored)
    return [t for score, t in scored if score == best]


async def send_manual_followup(session: AsyncSession, task: Task) -> None:
    """Send the normal reminder for this one task now (a reply in the task's email thread)."""
    await _send_reminder(session, task, utcnow())
    await session.commit()


async def run_followups(session: AsyncSession, now: Optional[datetime] = None, force: bool = False) -> dict:
    """One pass. Safe to run often (the scheduler runs it every ~15 minutes).

    force=True ignores working hours (used by the manual API trigger / tests).
    """
    s = get_settings()
    now = now or utcnow()
    stats = {"retried_delivery": 0, "reminders": 0, "escalated_silence": 0,
             "escalated_overdue": 0, "skipped": None}

    if not force and not is_work_time(now):
        stats["skipped"] = "outside working hours"
        return stats

    open_filter = Task.status != TaskStatus.DONE.value
    active_assignee = Task.assignee.has(Employee.active.is_(True))

    # 0) assignment emails that failed earlier
    # (the background sender normally handles this; this pass is a safety net)
    stats["retried_delivery"] = await delivery.deliver_pending(session, now=now)

    # 0b) status questions nobody answered: stop waiting, and send the alerts that were held back
    stale = now - timedelta(hours=s.status_check_hours)
    res = await session.execute(
        select(Task).where(Task.status_check == "unconfirmed", Task.last_reply_at <= stale))
    stats["status_checks_expired"] = 0
    for task in res.scalars().unique():
        task_id = task.id
        try:
            held = await task_service.last_status_detail(session, task_id)
            status = task.status
            await task_service.settle_status_check(
                session, task, "status_check_expired",
                f"no answer after {s.status_check_hours} h; keeping '{status}'")
            await session.commit()
            if status == TaskStatus.BLOCKED.value:
                await escalation.notify_blocker(session, task, held)
            elif status == TaskStatus.DONE.value:
                await escalation.notify_done(session, task, held)
            await session.commit()
            stats["status_checks_expired"] += 1
        except Exception as e:
            await session.rollback()
            log.error("expiring status check for task %s failed: %s", task_id, e)

    # 1) reminders due now (or escalate if too many went unanswered)
    res = await session.execute(
        select(Task).where(
            open_filter, active_assignee, Task.notified_at.is_not(None), Task.escalated.is_(False),
            Task.next_followup_at.is_not(None), Task.next_followup_at <= now,
        )
    )
    for task in res.scalars().unique():
        task_id = task.id  # read now: attributes expire after a rollback
        try:
            if task.followup_count >= s.max_unanswered:
                await escalation.escalate(
                    session, task,
                    f"no reply after {task.followup_count} reminders since "
                    f"{fmt_local(task.last_reply_at or task.created_at)}",
                )
                stats["escalated_silence"] += 1
            else:
                await _send_reminder(session, task, now)
                stats["reminders"] += 1
            await session.commit()
        except Exception as e:
            await session.rollback()
            log.error("follow-up failed for task %s: %s", task_id, e)

       # 2) overdue work nobody has dealt with
    cutoff = now - timedelta(hours=s.overdue_escalate_after_hours)
    res = await session.execute(
        select(Task).where(
            open_filter, active_assignee, Task.notified_at.is_not(None), Task.escalated.is_(False),
            Task.due_date.is_not(None), Task.due_date < cutoff,
        )
    )
    for task in res.scalars().unique():
        task_id = task.id
        try:
            await escalation.escalate(session, task, f"overdue since {fmt_local(task.due_date)}")
            stats["escalated_overdue"] += 1
            await session.commit()
        except Exception as e:
            await session.rollback()
            log.error("overdue escalation failed for task %s: %s", task_id, e)

    return stats