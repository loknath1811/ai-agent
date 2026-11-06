"""DANGER: delete ALL people, teams, divisions and ALL tasks (with their history and sent-email
records), so you can start again and add people from the dashboard's People page.

It KEEPS the log of emails the agent has already handled, so old emails in the agent's mailbox
are not processed a second time once you add people back. (scripts.reset_db deletes that log too.)

    python -m scripts.clear_org           # shows what would be deleted, deletes nothing
    python -m scripts.clear_org --yes     # really deletes
"""
import asyncio
import sys

from sqlalchemy import delete, func, select, update

from app.database import async_engine, async_session
from app.models import Base, Division, Employee, OrgChange, OutboundEmail, Task, TaskEvent, Team

COUNTED = [("people", Employee), ("teams", Team), ("divisions", Division),
           ("tasks", Task), ("task history entries", TaskEvent), ("sent-email records", OutboundEmail)]


async def main() -> None:
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_session() as s:
        counts = {name: (await s.execute(select(func.count()).select_from(model))).scalar_one()
                  for name, model in COUNTED}
        print("Currently in the database: " + ", ".join(f"{n} {name}" for name, n in counts.items()))
        if "--yes" not in sys.argv:
            await async_engine.dispose()
            sys.exit("Nothing deleted. Re-run with --yes to delete all of the above.")
        await s.execute(delete(TaskEvent))
        await s.execute(delete(OutboundEmail))
        await s.execute(delete(Task))
        await s.execute(update(Employee).values(manager_id=None))     # managers point at people
        await s.execute(delete(Employee))
        await s.execute(delete(Team))
        await s.execute(delete(Division))
        s.add(OrgChange(action="cleared", target="everything",
                        detail=", ".join(f"{n} {name}" for name, n in counts.items()) + " deleted"))
        await s.commit()
    await async_engine.dispose()
    print("Done. All people, teams, divisions and tasks are deleted. Add people from the dashboard's People page.")


asyncio.run(main())