"""Load divisions, teams and employees from a CSV file (safe to run again).

    python -m scripts.import_employees data/employees_sample.csv           # preview + save
    python -m scripts.import_employees data/employees_sample.csv --preview # preview only

Columns: division, team, full_name, email, title, manager_email, is_leader
Uses the same checks as the dashboard's People > Import. Nothing is saved if any row has an error.
"""
import asyncio
import sys

from app.database import async_engine, async_session
from app.models import Base
from app.services import org_import


async def main(path: str, preview_only: bool) -> None:
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    with open(path, newline="", encoding="utf-8-sig") as f:
        text = f.read()
    async with async_session() as s:
        try:
            out = await org_import.process(s, text, apply=not preview_only)
        except ValueError as e:
            await async_engine.dispose()
            sys.exit(str(e))
    for r in out["rows"]:
        msg = "; ".join(r["problems"] + r["notes"])
        print(f"  line {r['line']:>3}  {r['action']:<9} {r['email']}" + (f"   {msg}" if msg else ""))
    await async_engine.dispose()
    c = out["summary"]
    print(f"{c['add']} to add, {c['update']} to update, {c['unchanged']} unchanged, {c['error']} with errors.")
    if c["error"]:
        sys.exit("Nothing was saved. Fix the lines marked 'error' and run again.")
    print("Preview only - nothing saved." if preview_only else f"Imported {len(out['rows'])} rows from {path}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        sys.exit("usage: python -m scripts.import_employees <file.csv> [--preview]")
    asyncio.run(main(args[0], "--preview" in sys.argv))
