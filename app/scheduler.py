"""Background jobs: read the mailbox often, follow up / escalate periodically."""
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import get_settings
from .database import async_session
from .services import brief, delivery, followup, inbox

log = logging.getLogger(__name__)
scheduler = AsyncIOScheduler()
_delivery_task = None


async def inbox_job() -> None:
    try:
        stats = await inbox.process_inbox()
        if stats["fetched"]:
            log.info("inbox check: %s", stats)
    except Exception as e:
        log.error("inbox check failed: %s", e)


async def followup_job() -> None:
    try:
        async with async_session() as session:
            stats = await followup.run_followups(session)
        if any(v for k, v in stats.items() if k != "skipped"):
            log.info("follow-up pass: %s", stats)
    except Exception as e:
        log.error("follow-up pass failed: %s", e)


async def brief_job() -> None:
    try:
        async with async_session() as session:
            out = await brief.send_brief(session)
        log.info("morning brief: %s", out["message"] or [(r["to"], "sent" if r["ok"] else r["error"]) for r in out["results"]])
    except Exception as e:
        log.error("morning brief failed: %s", e)


def start_scheduler() -> None:
    s = get_settings()
    scheduler.add_job(inbox_job, "interval", seconds=s.inbox_poll_seconds, id="inbox",
                      max_instances=1, coalesce=True, replace_existing=True)
    scheduler.add_job(followup_job, "interval", minutes=s.followup_job_minutes, id="followups",
                      max_instances=1, coalesce=True, replace_existing=True)
    if s.brief_enabled:
        scheduler.add_job(brief_job, "cron", hour=s.brief_hour, minute=0, timezone=s.timezone,
                          day_of_week="mon-fri" if s.skip_weekends else "*", id="brief",
                          max_instances=1, coalesce=True, misfire_grace_time=3600, replace_existing=True)
    scheduler.start()
    global _delivery_task
    _delivery_task = asyncio.get_running_loop().create_task(delivery.delivery_loop())
    log.info("Scheduler started (inbox every %ss, follow-ups every %s min)",
             s.inbox_poll_seconds, s.followup_job_minutes)


def shutdown_scheduler() -> None:
    if _delivery_task:
        _delivery_task.cancel()
    if scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("Scheduler stopped.")
