"""Meeting notes -> a REVIEWABLE list of tasks.

preview_notes() reads the notes with ONE AI call and works out, for every proposed task, who
would receive it - but creates nothing. The leader checks and edits the list; only then does
confirm (the normal assign_from_items) create tasks and send emails. The AI never picks an
email address: people are matched by the same directory code as every other assignment.
"""
import re
from typing import List, Optional

from sqlalchemy.ext.asyncio import AsyncSession

from ..llm_tasks import AssignmentItem, NotesItem, parse_notes
from ..models import Employee
from ..timeutil import fmt_local, parse_due, utcnow
from .assign import resolve_targets

MAX_ITEMS = 25


def _squash(text: str) -> str:
    """Lowercase and collapse punctuation/spaces so a verbatim check survives tiny formatting changes."""
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _evidence_found(evidence: str, notes: str) -> bool:
    e = _squash(evidence)
    return bool(e) and e in _squash(notes)


async def _row(session: AsyncSession, actor: Employee, notes: str, n: int, item: NotesItem) -> dict:
    problem: Optional[str] = None
    recipients: List[dict] = []
    fyi: List[str] = []
    assignee = (item.assignee or "").strip()
    due = parse_due(item.due_date)

    if not assignee:
        problem = "No owner is named in the notes. Type who should do this."
    elif due is not None and due < utcnow():
        problem = f"The deadline {fmt_local(due)} has already passed. Pick a new date."
    else:
        people, problem, fyi = await resolve_targets(
            session, actor,
            AssignmentItem(title=item.title, assignee=assignee, description=item.description,
                           due_date=item.due_date, fan_out=item.fan_out),
        )
        recipients = [{"name": p.full_name, "email": p.email} for p in people]

    return {
        "index": n,
        "title": item.title.strip(),
        "assignee": assignee,
        "due_date": (item.due_date or "")[:10],
        "description": item.description,
        "fan_out": item.fan_out,
        "evidence": item.evidence.strip(),
        "verified": _evidence_found(item.evidence, notes),   # False = the AI may have invented it
        "recipients": recipients,
        "problem": problem,
        "note": " ".join(fyi) or None,
    }


async def preview_notes(session: AsyncSession, actor: Employee, notes: str) -> dict:
    parsed = await parse_notes(notes)
    items = parsed.items[:MAX_ITEMS]
    rows = [await _row(session, actor, notes, i, it) for i, it in enumerate(items)]
    return {
        "rows": rows,
        "truncated": len(parsed.items) > MAX_ITEMS,
    }
