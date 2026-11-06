"""Optional: put a few demo tasks in different states so the dashboard has something to show.
Run AFTER importing employees. Uses the people who are actually in your database,
so it works with any employees CSV.

Creating a task emails the assignee, so by default this script refuses to run unless
EMAIL_DRY_RUN=true. To really send the emails, add --send.

    python -m scripts.seed_demo
    python -m scripts.seed_demo --send
"""
import asyncio
import sys
from datetime import timedelta

from sqlalchemy import select

from app.config import get_settings
from app.database import async_engine, async_session
from app.models import Employee
from app.services import tasks as ts
from app.timeutil import utcnow

# (title, days until deadline, status to apply)
PLAN = [
    ("NCR root-cause review", 2, "on-track"),
    ("Supplier audit report", -2, "delayed"),
    ("Thermal model update", 5, "blocked"),
    ("Flight software checklist", 10, "done"),
    ("Design review deck", 3, "pending"),
]


async def main() -> None:
    if not get_settings().email_dry_run and "--send" not in sys.argv:
        raise SystemExit(
            "Creating demo tasks emails the assignees. Set EMAIL_DRY_RUN=true in .env "
            "to run safely, or re-run with --send if you really want the emails sent."
        )
    async with async_session() as s:
        emps = [e for e in (await s.execute(select(Employee).order_by(Employee.id))).scalars().unique()
                if e.active]
        if not emps:
            raise SystemExit("No employees found. Import your employees CSV first.")
        leaders = [e for e in emps if e.is_leader]
        boss = leaders[0] if leaders else emps[0]
        people = [e for e in emps if not e.is_leader] or [e for e in emps if e.id != boss.id]
        if not people:
            raise SystemExit("Need at least two employees to create demo tasks.")
        now = utcnow()
        for i, (title, days, status) in enumerate(PLAN):
            who = people[i % len(people)]
            t = await ts.create_task(s, title=title, assignee=who, assigned_by=boss,
                                     due_date=now + timedelta(days=days))
            if status != "pending":
                await ts.apply_status(s, t, status, actor_id=who.id, detail="seeded demo status")
                await s.commit()
            print(f"  #{t.id} {title} -> {who.full_name} ({status})")
    await async_engine.dispose()
    print(f"Demo tasks created (assigned by {boss.full_name}).")


asyncio.run(main())