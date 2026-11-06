"""Leader instruction -> real tasks. Resolves people through the directory
(the AI never chooses an email address) and asks when something is unclear."""
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from .. import directory
from ..llm_tasks import AssignmentItem
from ..models import Employee, Task
from ..timeutil import fmt_local, parse_due, utcnow
from . import tasks as task_service


@dataclass
class AssignmentResult:
    created: List[Task] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)     # FYI lines, e.g. "you were skipped"
    attached: List[str] = field(default_factory=list)  # names of files that go with every created task
    duplicates: List[Task] = field(default_factory=list)  # identical tasks that already existed (nothing new made)

    def summary(self) -> str:
        lines = []
        for t in self.created:
            lines.append(
                f"Created task #{t.id} '{t.title}' for {t.assignee.full_name} "
                f"({t.assignee.email}), deadline: {fmt_local(t.due_date)}."
                + (f" Attached files: {', '.join(self.attached)}." if self.attached else "")
                + (" The assignment email is being sent now." if t.delivery_status == "waiting" else "")
            )
        lines += [f"Could not assign: {p}" for p in self.problems]
        lines += [f"Note: {n}" for n in self.notes]
        return "\n".join(lines) if lines else "Nothing to assign."


async def resolve_targets(
    session: AsyncSession, assigner: Optional[Employee], item: AssignmentItem
) -> Tuple[List[Employee], Optional[str], List[str]]:
    """Who would receive this item? Returns (people, problem or None, FYI notes). Creates nothing,
    so the meeting-notes preview and the real assignment always agree."""
    notes: List[str] = []
    res = await directory.resolve(session, item.assignee)
    if res.kind == "person":
        return [res.employee], None, notes
    if res.kind == "team":
        team = res.team
        if item.fan_out:
            members = await directory.team_members(session, team.id)
            targets = [m for m in members if not (assigner and m.id == assigner.id)]
            if assigner and len(targets) < len(members):
                notes.append(
                    f"You are a member of {team.name}, so no task was created for you "
                    f"(you assigned it). Everyone else on the team received one."
                )
            if not targets:
                return [], f"Team '{team.name}' has no active members.", notes
            return targets, None, notes
        lead = await directory.team_leader(session, team.id)
        if not lead:
            return [], (
                f"Team '{team.name}' has no team leader set. Name a person, or say "
                f"'each member of {team.name}' to assign it to everyone."
            ), notes
        return [lead], None, notes
    # ambiguous or none -> ask, never guess
    return [], res.message or f"Unknown assignee '{item.assignee}'.", notes


async def assign_from_items(
    session: AsyncSession, assigner: Optional[Employee], items: List[AssignmentItem],
    attachment_ids: Optional[Sequence[int]] = None,
) -> AssignmentResult:
    """`attachment_ids`: files (already uploaded by the assigner) that go on every task created here."""
    result = AssignmentResult()
    for item in items:
        due = parse_due(item.due_date)
        if due is not None and due < utcnow():
            # never create a task that is already overdue: ask for a real deadline instead
            result.problems.append(
                f"'{item.title}' for {item.assignee}: the deadline {fmt_local(due)} has already passed, "
                f"so no task was created. Please give a new deadline (for example 'by next Friday' or 'by 30 Oct')."
            )
            continue
        targets, problem, notes = await resolve_targets(session, assigner, item)
        result.notes.extend(notes)
        if problem:
            result.problems.append(problem)
            continue

        for person in targets:
            task, was_duplicate = await task_service.create_task_once(
                session, title=item.title, assignee=person, assigned_by=assigner,
                description=item.description, due_date=due, attachment_ids=attachment_ids,
            )
            if was_duplicate:
                # Say so out loud: silently skipping would leave the leader thinking a NEW task went out.
                result.duplicates.append(task)
                mins = max(0, int((utcnow() - task.created_at).total_seconds() // 60))
                result.notes.append(
                    f"Task #{task.id} '{task.title}' was already assigned to {task.assignee.full_name} "
                    f"{'a moment' if mins < 1 else f'{mins} min'} ago, so I did not create a duplicate "
                    f"or send a second email."
                )
            else:
                result.created.append(task)
    return result
