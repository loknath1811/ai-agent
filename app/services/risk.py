"""Which open tasks are HEADING for trouble (not late yet)?  Plain rules - no AI - so every warning can be explained.

A task is "at risk" when it is open, not yet overdue, not escalated, and at least one of these is true:
  1. it is due within 2 days, the assignment email went out, and the assignee has not replied;
  2. it is due within 2 days and its status is already "delayed" or "blocked";
  3. two or more reminders have gone unanswered.
Tasks that are already overdue or escalated are not "at risk" - they are already a problem, and are listed as such.
"""
from datetime import datetime, timedelta
from typing import List

from ..models import Task, TaskStatus

SOON_DAYS = 2
UNANSWERED_REMINDERS = 2


def risk_reasons(t: Task, now: datetime) -> List[str]:
    if t.status == TaskStatus.DONE.value or t.escalated:
        return []
    if t.due_date is not None and t.due_date < now:       # already overdue
        return []
    reasons: List[str] = []
    due_soon = t.due_date is not None and t.due_date <= now + timedelta(days=SOON_DAYS)
    if due_soon and t.status == TaskStatus.PENDING.value and t.last_reply_at is None and t.notified_at is not None:
        reasons.append("due soon and no reply yet")
    if due_soon and t.status in (TaskStatus.DELAYED.value, TaskStatus.BLOCKED.value):
        reasons.append(f"due soon but marked {t.status}")
    if (t.followup_count or 0) >= UNANSWERED_REMINDERS and t.last_reply_at is None:
        reasons.append(f"{t.followup_count} reminders with no reply")
    return reasons
