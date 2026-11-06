"""Impact numbers: what the agent did, how fast, and a clearly-labelled estimate of time saved.

Everything is counted from records the app already keeps (task history, sent-email log, inbound-email log).
Nothing is guessed except the single "time saved" figure, which multiplies real counts by an ASSUMPTION
(MANUAL_STEP_MINUTES) that is shown next to the number.
"""
import statistics
from collections import Counter
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import EmailLog, OutboundEmail, Task, TaskEvent, TaskStatus
from ..timeutil import utcnow

BLOCKER_PAIR_WINDOW = timedelta(minutes=15)   # a "blocked" status and its alert belong together if this close


def _median(values: List[float]) -> Optional[float]:
    return round(statistics.median(values), 2) if values else None


async def compute(session: AsyncSession, days: int = 7, now: Optional[datetime] = None) -> Dict:
    s = get_settings()
    now = now or utcnow()
    since = now - timedelta(days=days)

    # ---------------------------------------------------------------- history events in the period
    events = list((await session.execute(
        select(TaskEvent).where(TaskEvent.created_at >= since, TaskEvent.created_at <= now)
        .order_by(TaskEvent.created_at))).scalars())
    by_type = Counter(e.event_type for e in events)
    created = {e.task_id: e.created_at for e in events if e.event_type == "created"}

    # ---------------------------------------------------------------- tasks
    done_tasks = list((await session.execute(
        select(Task).where(Task.completed_at >= since, Task.completed_at <= now,
                           Task.status == TaskStatus.DONE.value))).scalars().unique())
    with_deadline = [t for t in done_tasks if t.due_date is not None]
    on_time = sum(1 for t in with_deadline if t.completed_at <= t.due_date)
    open_now = (await session.execute(
        select(func.count()).select_from(Task).where(Task.status != TaskStatus.DONE.value))).scalar_one()

    # ---------------------------------------------------------------- emails the agent sent
    kinds = dict((await session.execute(
        select(OutboundEmail.kind, func.count()).where(OutboundEmail.sent_at >= since, OutboundEmail.sent_at <= now)
        .group_by(OutboundEmail.kind))).all())

    # ---------------------------------------------------------------- inbound emails the agent read
    async def inbound(pattern: str) -> int:
        return (await session.execute(
            select(func.count()).select_from(EmailLog).where(
                EmailLog.status == "processed", EmailLog.detail.like(pattern),
                EmailLog.created_at >= since, EmailLog.created_at <= now))).scalar_one()
    replies_read = await inbound("task %")                # an assignee's reply, or a manager's comment, on a task
    assignments_by_email = await inbound("leader assignment%")

    # ---------------------------------------------------------------- speed 1: time to the assignee's first response
    first_reply_hours: List[float] = []
    if created:
        assignee = dict((await session.execute(
            select(Task.id, Task.assignee_id).where(Task.id.in_(list(created))))).all())
        later = (await session.execute(
            select(TaskEvent).where(TaskEvent.task_id.in_(list(created)),
                                    TaskEvent.event_type.in_(["status_change", "comment"]))
            .order_by(TaskEvent.created_at))).scalars()
        seen = set()
        for e in later:
            if e.task_id in seen or e.actor_id is None or e.actor_id != assignee.get(e.task_id):
                continue
            if e.created_at > created[e.task_id]:
                seen.add(e.task_id)
                first_reply_hours.append((e.created_at - created[e.task_id]).total_seconds() / 3600)
    replied_pct = round(100 * len(first_reply_hours) / len(created)) if created else None

    # ---------------------------------------------------------------- speed 2: blocker read -> managers emailed
    blocked_at: Dict[int, List[datetime]] = {}
    for e in events:
        if e.event_type == "status_change" and e.new_status == "blocked":
            blocked_at.setdefault(e.task_id, []).append(e.created_at)
    alert_seconds: List[float] = []
    for e in events:
        if e.event_type != "blocker_reported":
            continue
        before = [b for b in blocked_at.get(e.task_id, []) if timedelta(0) <= e.created_at - b <= BLOCKER_PAIR_WINDOW]
        if before:
            alert_seconds.append((e.created_at - max(before)).total_seconds())

    # ---------------------------------------------------------------- how often was the AI's reading right?
    waiting_now = (await session.execute(
        select(func.count()).select_from(Task).where(Task.status_check == "unconfirmed"))).scalar_one()
    confirmed, corrected = by_type["status_confirmed"], by_type["status_corrected"]
    checked = confirmed + corrected

    # ---------------------------------------------------------------- the one estimate
    reminders = kinds.get("reminder", 0)
    blocker_alerts = kinds.get("blocker", 0)
    escalation_emails = kinds.get("escalation", 0)
    steps = reminders + replies_read + by_type["blocker_reported"] + by_type["escalated"]
    minutes = steps * s.manual_step_minutes

    return {
        "days": days,
        "since": since.isoformat(timespec="seconds"),
        "tasks": {
            "assigned": by_type["created"],
            "completed": len(done_tasks),
            "completed_with_deadline": len(with_deadline),
            "completed_on_time": on_time,
            "on_time_pct": round(100 * on_time / len(with_deadline)) if with_deadline else None,
            "open_now": open_now,
        },
        "automation": {
            "assignment_emails_delivered": by_type["delivered"],
            "reminders_sent": reminders,
            "replies_read": replies_read,
            "assignments_received_by_email": assignments_by_email,
            "blocker_alerts": by_type["blocker_reported"],
            "blocker_alert_emails": blocker_alerts,
            "escalations": by_type["escalated"],
            "escalation_emails": escalation_emails,
            "emails_sent_total": sum(kinds.values()),
        },
        "speed": {
            "first_reply_median_hours": _median(first_reply_hours),
            "first_reply_samples": len(first_reply_hours),
            "replied_pct": replied_pct,
            "tasks_assigned_in_period": len(created),
            "blocker_alert_median_seconds": _median(alert_seconds),
            "blocker_alert_samples": len(alert_seconds),
            "mailbox_check_seconds": s.inbox_poll_seconds,
        },
        "accuracy": {
            "asked": by_type["status_check_asked"],          # the AI was unsure and the employee was asked
            "confirmed": confirmed,                          # "yes, that is right" (employee or manager)
            "corrected": corrected,                          # a different status was given instead
            "expired": by_type["status_check_expired"],      # nobody answered in time
            "waiting_now": waiting_now,
            "checked": checked,
            "right_pct": round(100 * confirmed / checked) if checked else None,
        },
        "time_saved": {
            "steps": steps,
            "minutes_per_step": s.manual_step_minutes,
            "minutes": minutes,
            "hours": round(minutes / 60, 1),
            "basis": "reminders sent + replies read + blocker alerts + escalations, each counted as one manual step",
        },
    }
