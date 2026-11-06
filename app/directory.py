"""Who works here. Turns what a leader types ("Ravi", "QA team", an email)
into real employees - and NEVER guesses when it is unclear."""
import difflib
import re
from dataclasses import dataclass, field
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import Division, Employee, Team

_STOP = {"the", "team", "pod", "division", "dept", "department", "group",
         "from", "in", "of", "a", "an", "at", "on", "to", "ask", "tell", "and"}


def _tokens(text: str) -> List[str]:
    return [t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _STOP]


def _norm(text: str) -> str:
    return " ".join(_tokens(text))


@dataclass
class Resolution:
    kind: str                                   # person | team | ambiguous | none
    employee: Optional[Employee] = None
    team: Optional[Team] = None
    candidates: List[Employee] = field(default_factory=list)
    message: str = ""


# ---------- simple lookups ----------
async def get_by_email(session: AsyncSession, email: str) -> Optional[Employee]:
    email = (email or "").strip().lower()
    if not email:
        return None
    res = await session.execute(select(Employee).where(Employee.email == email))
    return res.scalars().first()


async def get_by_id(session: AsyncSession, emp_id: Optional[int]) -> Optional[Employee]:
    if emp_id is None:
        return None
    return await session.get(Employee, emp_id)


async def active_employees(session: AsyncSession) -> List[Employee]:
    res = await session.execute(select(Employee).where(Employee.active.is_(True)).order_by(Employee.full_name))
    return list(res.scalars().unique())


async def team_members(session: AsyncSession, team_id: int) -> List[Employee]:
    res = await session.execute(
        select(Employee).where(Employee.team_id == team_id, Employee.active.is_(True)).order_by(Employee.full_name)
    )
    return list(res.scalars().unique())


async def team_leader(session: AsyncSession, team_id: int) -> Optional[Employee]:
    res = await session.execute(
        select(Employee)
        .where(Employee.team_id == team_id, Employee.is_leader.is_(True), Employee.active.is_(True))
        .order_by(Employee.id)
    )
    return res.scalars().first()


async def find_team(session: AsyncSession, name: str) -> Optional[Team]:
    res = await session.execute(select(Team))
    teams = list(res.scalars().unique())
    want = _norm(name)
    for t in teams:
        if _norm(t.name) == want or t.name.lower() == (name or "").strip().lower():
            return t
    return None


async def find_division(session: AsyncSession, name: str) -> Optional[Division]:
    res = await session.execute(select(Division))
    want = _norm(name)
    for d in res.scalars().unique():
        if _norm(d.name) == want or d.name.lower() == (name or "").strip().lower():
            return d
    return None


# ---------- the main resolver ----------
async def resolve(session: AsyncSession, query: str) -> Resolution:
    """email -> exact team -> person by name (optionally 'Ravi from QA')."""
    q = (query or "").strip()
    if not q:
        return Resolution("none", message="No assignee given.")

    # 1) an email address is always exact
    m = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", q)
    if m:
        emp = await get_by_email(session, m.group(0))
        if emp and emp.active:
            return Resolution("person", employee=emp)
        return Resolution("none", message=f"I don't have an active employee with email {m.group(0)}.")

    # 2) a whole team ("QA team")
    team = await find_team(session, q)
    if team:
        return Resolution("team", team=team)

    # 3) a person by name
    employees = await active_employees(session)
    q_tokens = _tokens(q)
    if not q_tokens:
        return Resolution("none", message=f"I couldn't understand who '{q}' is.")

    exact = [e for e in employees if _norm(e.full_name) == " ".join(q_tokens)]
    if len(exact) == 1:
        return Resolution("person", employee=exact[0])

    team_names = {t: _tokens(t) for t in {e.team.name for e in employees if e.team}}
    team_hint: Optional[str] = None
    name_tokens: List[str] = []
    for tok in q_tokens:
        hit = next((tn for tn, toks in team_names.items() if tok in toks), None)
        if hit and name_tokens:            # "Ravi QA": later tokens can be a team hint
            team_hint = hit
        else:
            name_tokens.append(tok)

    def matches(e: Employee) -> bool:
        parts = _tokens(e.full_name)
        if not all(any(p.startswith(t) for p in parts) for t in name_tokens):
            return False
        return team_hint is None or (e.team is not None and e.team.name == team_hint)

    found = [e for e in employees if matches(e)]
    if len(found) == 1:
        return Resolution("person", employee=found[0])
    if len(found) > 1:
        options = "; ".join(f"{e.full_name} ({e.team.name if e.team else 'no team'}, {e.email})" for e in found)
        return Resolution(
            "ambiguous", candidates=found,
            message=f"'{q}' matches more than one person: {options}. Which one did you mean?",
        )

    close = difflib.get_close_matches(_norm(q), [_norm(e.full_name) for e in employees], n=3, cutoff=0.6)
    hint = ""
    if close:
        names = [e.full_name for e in employees if _norm(e.full_name) in close]
        hint = f" Did you mean: {', '.join(names)}?"
    return Resolution("none", message=f"I couldn't find anyone called '{q}'.{hint}")


# ---------- who to notify above someone ----------
async def escalation_chain(session: AsyncSession, emp: Employee, max_depth: int = 3) -> List[Employee]:
    """Direct manager, up the chain, plus the team leader. No duplicates, not the person."""
    chain: List[Employee] = []
    seen = {emp.id}
    cur, depth = emp, 0
    while cur.manager_id and depth < max_depth:
        mgr = await get_by_id(session, cur.manager_id)
        if not mgr or mgr.id in seen:
            break
        if mgr.active:
            chain.append(mgr)
        seen.add(mgr.id)
        cur, depth = mgr, depth + 1
    if emp.team_id:
        lead = await team_leader(session, emp.team_id)
        if lead and lead.id not in seen and lead.active:
            chain.append(lead)
    return chain
