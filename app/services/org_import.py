"""Bulk loading of divisions, teams and people from CSV text.

Always preview first: `process(session, text, apply=False)` checks every row and says what would
happen (add / update / unchanged / error) without touching the database. `apply=True` does the
same checks and then saves - but ONLY when there are no errors (all or nothing), so a half-loaded
organization can never happen. People missing from the file are left alone (never auto-removed).

Columns: division, team, full_name, email, title, manager_email, is_leader
"""
import csv
import io
import re
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..models import Division, Employee, OrgChange, Team

EMAIL_RE = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
TRUE = {"1", "true", "yes", "y"}
FALSE = {"", "0", "false", "no", "n"}
MAX_ROWS = 5000


def truthy(v: Optional[str]) -> bool:
    return (v or "").strip().lower() in TRUE


def email_problem(raw: Optional[str]) -> Optional[str]:
    """Why this address cannot be used for an employee (None = fine)."""
    e = (raw or "").strip().lower()
    if not EMAIL_RE.match(e):
        return f"'{(raw or '').strip()}' is not a valid email address"
    agent = get_settings().smtp_user.strip().lower()
    if agent and e == agent:
        return "this is the agent's own mailbox, so it cannot also be an employee"
    return None


@dataclass
class RowResult:
    line: int                      # line number in the file (the header is line 1)
    name: str
    email: str
    action: str = "add"            # add | update | unchanged | error
    problems: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)


def parse(csv_text: str) -> List[dict]:
    reader = csv.DictReader(io.StringIO((csv_text or "").lstrip("﻿")))
    if reader.fieldnames is None:
        raise ValueError("The file is empty.")
    reader.fieldnames = [(f or "").strip().lower() for f in reader.fieldnames]
    missing = {"full_name", "email"} - set(reader.fieldnames)
    if missing:
        raise ValueError("The file must have these columns: division, team, full_name, email, title, "
                         f"manager_email, is_leader. Missing: {', '.join(sorted(missing))}.")
    rows = []
    for i, r in enumerate(reader, start=2):
        clean = {k: (v or "").strip() for k, v in r.items() if k}
        if not any(clean.values()):
            continue                                   # skip blank lines
        clean["_line"] = i
        rows.append(clean)
        if len(rows) > MAX_ROWS:
            raise ValueError(f"Too many rows (limit {MAX_ROWS}).")
    if not rows:
        raise ValueError("The file has no people in it.")
    return rows


async def process(session: AsyncSession, csv_text: str, apply: bool = False) -> dict:
    rows = parse(csv_text)

    db_emps = list((await session.execute(select(Employee))).scalars().unique())
    by_email: Dict[str, Employee] = {e.email: e for e in db_emps}
    email_of_id = {e.id: e.email for e in db_emps}
    divs = {d.name.lower(): d for d in (await session.execute(select(Division))).scalars().unique()}
    teams = {t.name.lower(): t for t in (await session.execute(select(Team))).scalars().unique()}

    results: List[RowResult] = []
    first_line: Dict[str, int] = {}
    file_emails = set()
    for r in rows:
        em = r.get("email", "").lower()
        res = RowResult(line=r["_line"], name=r.get("full_name", ""), email=em)
        results.append(res)
        if not res.name:
            res.problems.append("full_name is empty")
        p = email_problem(em)
        if p:
            res.problems.append(p)
        elif em in first_line:
            res.problems.append(f"email appears twice (also on line {first_line[em]})")
        else:
            first_line[em] = res.line
            file_emails.add(em)
        flag = r.get("is_leader", "").lower()
        if flag not in TRUE and flag not in FALSE:
            res.problems.append(f"is_leader must be true or false (got '{r.get('is_leader')}')")

    # division / team checks
    new_divs, new_teams = set(), set()
    for r, res in zip(rows, results):
        dname, tname = r.get("division", ""), r.get("team", "")
        if tname and dname and tname.lower() in teams:
            existing = teams[tname.lower()]
            ex_div = existing.division.name if existing.division else None
            if ex_div and ex_div.lower() != dname.lower():
                res.problems.append(f"team '{existing.name}' already belongs to division '{ex_div}', not '{dname}'")
        if dname and dname.lower() not in divs and dname.lower() not in new_divs:
            new_divs.add(dname.lower())
            res.notes.append(f"new division '{dname}' will be created")
        if tname and tname.lower() not in teams and tname.lower() not in new_teams:
            new_teams.add(tname.lower())
            res.notes.append(f"new team '{tname}' will be created")

    # manager checks
    final_mgr: Dict[str, Optional[str]] = {e.email: email_of_id.get(e.manager_id) for e in db_emps}
    for r in rows:
        final_mgr[r.get("email", "").lower()] = r.get("manager_email", "").lower() or None
    for r, res in zip(rows, results):
        m = r.get("manager_email", "").lower()
        if not m:
            continue
        if email_problem(m) and not (m in by_email or m in file_emails):
            res.problems.append(f"manager_email '{r['manager_email']}' is not a valid email address")
        elif m == res.email:
            res.problems.append("a person cannot be their own manager")
        elif m not in file_emails and m not in by_email:
            res.problems.append(f"manager {m} is not in the file or in the directory")
        elif m not in file_emails and not by_email[m].active:
            res.problems.append(f"manager {m} is inactive")
        else:
            seen, cur = {res.email}, m              # walk up: must never come back to this person
            while cur:
                if cur in seen:
                    res.problems.append("this creates a management loop")
                    break
                seen.add(cur)
                cur = final_mgr.get(cur)

    # what would happen to each row
    for r, res in zip(rows, results):
        if res.problems:
            res.action = "error"
            continue
        e = by_email.get(res.email)
        if not e:
            res.action = "add"
            continue
        same = (e.full_name == res.name and (e.title or "") == r.get("title", "")
                and (e.team.name if e.team else "").lower() == r.get("team", "").lower()
                and (email_of_id.get(e.manager_id) or "") == r.get("manager_email", "").lower()
                and e.is_leader == truthy(r.get("is_leader")) and e.active)
        res.action = "unchanged" if same else "update"
        if not e.active:
            res.notes.append("was inactive - will be reactivated")

    counts = {k: sum(1 for x in results if x.action == k) for k in ("add", "update", "unchanged", "error")}
    out = {"rows": [asdict(x) for x in results], "summary": counts, "applied": False}
    if not apply or counts["error"]:
        return out

    # ---------------- apply (no errors) ----------------
    async def division(name):
        if not name:
            return None
        d = divs.get(name.lower())
        if not d:
            d = Division(name=name)
            session.add(d)
            await session.flush()
            divs[name.lower()] = d
        return d

    async def team(name, div):
        if not name:
            return None
        t = teams.get(name.lower())
        if not t:
            t = Team(name=name, division_id=div.id if div else None)
            session.add(t)
            await session.flush()
            teams[name.lower()] = t
        elif div and t.division_id is None:
            t.division_id = div.id
        return t

    people: Dict[str, Employee] = {}
    for r, res in zip(rows, results):
        d = await division(r.get("division", ""))
        t = await team(r.get("team", ""), d)
        e = by_email.get(res.email)
        if not e:
            e = Employee(email=res.email, full_name=res.name)
            session.add(e)
        e.full_name = res.name
        e.title = r.get("title") or None
        e.team_id = t.id if t else None
        e.is_leader = truthy(r.get("is_leader"))
        e.active = True
        people[res.email] = e
    await session.flush()
    for r, res in zip(rows, results):
        m = r.get("manager_email", "").lower()
        mgr = people.get(m) or by_email.get(m) if m else None
        people[res.email].manager_id = mgr.id if mgr else None
    session.add(OrgChange(
        action="import", target=f"{len(rows)} rows",
        detail=f"{counts['add']} added, {counts['update']} updated, {counts['unchanged']} unchanged"))
    await session.commit()
    out["applied"] = True
    return out
