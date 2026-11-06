"""Background sending of assignment emails.

Creating a task only saves it (delivery_status = "waiting") and wakes this sender.
The sender emails the assignee, then marks the task "sent". If Gmail fails it retries
after 1, 5 and 15 minutes, then marks the task "failed" so the dashboard can show it.

Several copies of the sender can run at once without emailing twice: a task is
"claimed" (its next-attempt time is pushed back) in a single database UPDATE before
anything is sent, and only the copy whose UPDATE matched a row may send.
"""
import asyncio
import logging
from datetime import timedelta
from typing import Optional

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from ..database import async_session
from ..models import Task, TaskStatus
from ..timeutil import utcnow
from . import tasks as task_service

log = logging.getLogger(__name__)

CLAIM_LEASE_SECONDS = 120      # if a sender dies mid-send, the task is retried after this
FALLBACK_SECONDS = 60          # look for due work this often even if nobody woke the sender
BATCH = 20

_wake = asyncio.Event()


def kick() -> None:
    """Tell the sender there is new work (called right after a task is saved)."""
    _wake.set()


async def deliver_pending(session: AsyncSession, now=None, limit: int = BATCH) -> int:
    """Send every assignment email that is due. Returns how many were delivered."""
    now = now or utcnow()
    due = or_(Task.delivery_next_at.is_(None), Task.delivery_next_at <= now)
    res = await session.execute(
        select(Task.id).where(Task.delivery_status == "waiting", due).order_by(Task.id).limit(limit)
    )
    delivered = 0
    for task_id in [r[0] for r in res.all()]:
        # claim: only one sender can win this UPDATE
        claim = await session.execute(
            update(Task)
            .where(Task.id == task_id, Task.delivery_status == "waiting", due)
            .values(delivery_next_at=now + timedelta(seconds=CLAIM_LEASE_SECONDS))
        )
        await session.commit()
        if claim.rowcount != 1:
            continue
        try:
            task = (await session.execute(
                select(Task).where(Task.id == task_id).execution_options(populate_existing=True)
            )).scalars().unique().one()
            if task.status == TaskStatus.DONE.value or not task.assignee.active:
                task.delivery_status = "failed"
                task.delivery_error = "not sent: task is closed or the assignee is inactive"
                task.delivery_next_at = None
                await task_service.log_event(session, task.id, "delivery_failed", detail=task.delivery_error)
            elif await task_service.deliver_assignment(session, task):
                delivered += 1
            await session.commit()
        except Exception as e:
            await session.rollback()
            log.error("delivery of task %s crashed: %s", task_id, e)
    return delivered


async def delivery_loop() -> None:
    """Runs for the life of the app: wake on new work, or every FALLBACK_SECONDS for retries."""
    log.info("Delivery sender started.")
    while True:
        try:
            await asyncio.wait_for(_wake.wait(), timeout=FALLBACK_SECONDS)
        except asyncio.TimeoutError:
            pass
        _wake.clear()
        try:
            async with async_session() as session:
                n = await deliver_pending(session)
            if n:
                log.info("delivery: %d assignment email(s) sent", n)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("delivery pass failed: %s", e)
