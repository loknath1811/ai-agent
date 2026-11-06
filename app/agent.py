"""Chat agent: a leader talks to it in plain language.

Tools are thin wrappers over the same services the email flow uses, so the
AI never picks email addresses or writes to the database directly.
"""
import asyncio
import contextvars
from typing import List, Optional

from langchain.agents import create_agent
from langchain_core.tools import tool

from . import directory, metrics
from .config import get_settings
from .database import async_session
from .llm_client import get_chat_model
from .llm_tasks import LLMError, parse_assignment
from .models import Employee
from .services import attachments as attachment_service
from .services import idempotency
from .services import followup as followup_service, reports, tasks as task_service
from .services.assign import assign_from_items
from .timeutil import fmt_local

MAX_HISTORY_TURNS = 20

# Tasks created while answering ONE chat message, so the page can show their email progress.
_created_tasks: contextvars.ContextVar = contextvars.ContextVar("created_tasks", default=None)


# State of the chat message being answered right now: the leader's own words, how many times the assign tool
# has run, and which instructions it has already handled in this turn (so a model that calls the tool twice
# with the same words does not create the task twice).
_turn: contextvars.ContextVar = contextvars.ContextVar("chat_turn", default=None)


def collect_created_tasks() -> list:
    """Call before chat(): returns a list that fills with {id, title, assignee} for every task created."""
    sink: list = []
    _created_tasks.set(sink)
    return sink

SYSTEM_PROMPT = (
    "You are the work-delegation assistant for Dhruva Space. You help leaders assign work "
    "and see status.\n"
    "- To assign or delegate work, ALWAYS call assign_work with the leader's request "
    "(copy their words; do not change names or invent email addresses).\n"
    "- To follow up with, remind or nudge a person about a task they ALREADY have, call "
    "send_followup with the person's name and the words that describe the task. NEVER use "
    "assign_work for a follow-up, reminder or nudge.\n"
    "- If the user gives only a name and no task, leave task_hint empty. Never invent it.\n"
    "- If send_followup asks which task, pass its question to the user word for word.\n"
    "- To answer 'what is the status of ...' call status_report.\n"
    "- For one specific task number call task_status.\n"
    "- If a tool says NO NEW TASK WAS CREATED, the request was already handled: tell the user so in "
    "plain words, repeat what the tool says, and do not call assign_work again.\n"
    "- If a tool says REFUSED, tell the user that message in plain words. Do not ask who "
    "they are or ask for a leader's name or email.\n"
    "- If the leader attached files, they are emailed automatically with every task that "
    "assign_work creates. Do not ask for the files and do not say you cannot send attachments.\n"
    "- Never claim an action happened unless a tool confirmed it. Keep answers short."
)


def build_agent(actor: Optional[Employee], attachment_ids: Optional[List[int]] = None):
    """Create an agent whose tools act on behalf of `actor` (the person chatting).
    `attachment_ids`: files the leader attached to this message; assign_work sends them with each new task."""

    async def _assign(instruction: str, turn: Optional[dict], call_no: int) -> str:
        if actor is None or not actor.is_leader:
            return "REFUSED: only leaders can assign work, and you are not a leader. No task was created."
        files: list = []
        if attachment_ids:
            async with async_session() as session:
                try:
                    files = await attachment_service.load_for_actor(session, actor, attachment_ids)
                except attachment_service.AttachmentError as e:
                    return f"No task was created. {e}"

        # Have we already acted on this exact request? Keyed on what the leader TYPED, not on how the AI words
        # the title or deadline (that varies from run to run). call_no keeps two different instructions
        # inside one message from being mistaken for each other.
        fp = idempotency.fingerprint("chat", actor.id, turn["message"] if turn else instruction, call_no)
        async with async_session() as cs:
            claim = await idempotency.claim(cs, fp, window_minutes=get_settings().duplicate_window_minutes)
        if claim.repeat:
            return ("NO NEW TASK WAS CREATED: this exact request was already handled "
                    f"{claim.age_text()}. " + (claim.summary or "It is still being processed."))

        saved = False
        try:
            try:
                parsed = await parse_assignment(instruction)
            except LLMError as e:
                return f"I couldn't understand that instruction ({e}). Please rephrase."
            if not parsed.is_assignment or not parsed.items:
                return "That doesn't look like a work assignment."
            async with async_session() as session:
                leader = await directory.get_by_id(session, actor.id)
                result = await assign_from_items(session, leader, parsed.items,
                                                 attachment_ids=[a.id for a in files])
                result.attached = [a.filename for a in files]
            sink = _created_tasks.get()
            if sink is not None:
                sink.extend({"id": t.id, "title": t.title, "assignee": t.assignee.full_name} for t in result.created)
            summary = result.summary()
            if result.created or result.duplicates:
                async with async_session() as fs:
                    await idempotency.finish(fs, claim.key, summary)      # a repeat will get this same answer
                saved = True
            return summary
        finally:
            if not saved:                       # nothing created (or it crashed): the leader may simply try again
                async with async_session() as rs:
                    await idempotency.release(rs, claim.key)

    @tool
    async def assign_work(instruction: str) -> str:
        """Assign work to a person or team from a plain-language instruction.
        Example: 'Ask the QA team to complete the NCR review by Friday'."""
        turn = _turn.get()
        if turn is None:
            return await _assign(instruction, None, 1)
        norm = idempotency.normalize_text(instruction)
        if norm in turn["seen"]:
            # The model called this tool AGAIN with the same words in the same turn: do not run it again.
            first = await turn["seen"][norm]
            return "NO NEW TASK WAS CREATED: you already ran this exact instruction in this same step. " + first
        turn["calls"] += 1
        run = asyncio.ensure_future(_assign(instruction, turn, turn["calls"]))
        turn["seen"][norm] = run
        return await run

    @tool
    async def status_report(scope: str, name: str = "") -> str:
        """Status summary. scope is one of: person, team, division, company.
        name is the person/team/division name (empty for company)."""
        scope = (scope or "").strip().lower()
        async with async_session() as session:
            if scope == "company":
                r = await reports.company_report(session)
                lines = [reports.describe("Company", r["summary"])]
                lines += [reports.describe(d["name"], d["summary"]) for d in r["divisions"]]
                return "\n".join(lines)
            if scope == "division":
                d = await directory.find_division(session, name)
                if not d:
                    return f"No division called '{name}'."
                r = await reports.division_report(session, d.id)
                return "\n".join([reports.describe(d.name, r["summary"])] +
                                 [reports.describe(t["name"], t["summary"]) for t in r["teams"]])
            if scope == "team":
                t = await directory.find_team(session, name)
                if not t:
                    return f"No team called '{name}'."
                r = await reports.team_report(session, t.id)
                return "\n".join([reports.describe(t.name, r["summary"])] +
                                 [reports.describe(m["name"], m["summary"]) for m in r["members"]])
            if scope == "person":
                res = await directory.resolve(session, name)
                if res.kind != "person":
                    return res.message or f"'{name}' is not a single person."
                r = await reports.person_report(session, res.employee.id)
                lines = [reports.describe(r["name"], r["summary"])]
                lines += [f"  #{t['id']} {t['title']} - {t['status']}"
                          + (" (OVERDUE)" if t["overdue"] else "") for t in r["tasks"][:15]]
                return "\n".join(lines)
        return "scope must be person, team, division or company."

    @tool
    async def task_status(task_id: int) -> str:
        """Current status of one task by its number."""
        async with async_session() as session:
            t = await task_service.get_task(session, task_id)
            if not t:
                return f"No task #{task_id}."
            return (f"Task #{t.id} '{t.title}' for {t.assignee.full_name}: {t.status}"
                    + (", ESCALATED" if t.escalated else ""))

    def _ask_which(name: str, tasks: list, intro: str) -> str:
        lines = [intro]
        lines += [f"  - {t.title} (due {fmt_local(t.due_date)}, status: {t.status})" for t in tasks]
        lines.append(
            f"Which task would you like me to follow up on? Please tell me the person and the task "
            f"together, for example: 'Send a follow-up to {name} about {tasks[0].title}'."
        )
        return "\n".join(lines)

    @tool
    async def send_followup(person: str, task_hint: str = "") -> str:
        """Send a follow-up (reminder) email to ONE person about a task they already have.
        person: the person's name exactly as the user wrote it.
        task_hint: the words the user used to describe the task (for example 'Siemens dashboard').
        Leave task_hint EMPTY if the user did not say which task. Never guess it.
        Use this for 'follow up', 'remind', 'nudge' or 'chase'. Never use assign_work for these."""
        if actor is None or not actor.is_leader:
            return "REFUSED: only leaders can send follow-ups, and you are not a leader. Nothing was sent."
        async with async_session() as session:
            res = await directory.resolve(session, person)
            if res.kind != "person":
                return res.message or f"'{person}' is not a single person. Please give one person's name."
            name = res.employee.full_name
            tasks = await followup_service.open_tasks_for(session, res.employee.id)
            if not tasks:
                return f"{name} has no open tasks, so there is nothing to follow up on."
            if not (task_hint or "").strip():
                return _ask_which(name, tasks, f"{name} currently has these open tasks:")
            picked = followup_service.match_tasks(tasks, task_hint)
            if not picked:
                return _ask_which(name, tasks, f"I could not find a task matching '{task_hint}'. "
                                               f"{name} currently has these open tasks:")
            if len(picked) > 1:
                return _ask_which(name, picked, f"More than one of {name}'s tasks matches '{task_hint}':")
            task = picked[0]
            title = task.title
            try:
                await followup_service.send_manual_followup(session, task)
            except Exception as e:
                await session.rollback()
                return f"I could not send the follow-up email to {name} ({type(e).__name__}: {e})."
        return f"Follow-up sent to {name} about '{title}'."

    tools: List = [assign_work, status_report, task_status, send_followup]
    return create_agent(model=get_chat_model(), tools=tools, system_prompt=SYSTEM_PROMPT)


async def chat(message: str, as_email: Optional[str] = None, history: Optional[list] = None,
               attachment_ids: Optional[List[int]] = None) -> str:
    """`history` = earlier turns of this conversation: [{"role": "user"|"assistant", "content": "..."}].
    `attachment_ids` = files uploaded for this message (they travel with any task it creates)."""
    actor = None
    names: List[str] = []
    if as_email:
        async with async_session() as session:
            actor = await directory.get_by_email(session, as_email)
            if actor is not None and attachment_ids:
                try:
                    names = [a.filename for a in await attachment_service.load_for_actor(session, actor, attachment_ids)]
                except attachment_service.AttachmentError:
                    names = []          # assign_work reports the problem itself; the model just isn't told about files
    agent = build_agent(actor, attachment_ids)

    messages = []
    for turn in history or []:
        role = str(turn.get("role", "user")).lower()
        # only real chat roles are accepted; anything else (e.g. a forged "system" message)
        # sent from the browser is dropped instead of being treated as a user instruction
        if role in ("assistant", "agent", "bot", "ai"):
            norm = "assistant"
        elif role in ("user", "human"):
            norm = "user"
        else:
            continue
        text = str(turn.get("content") or turn.get("text") or turn.get("message") or "").strip()
        if text:
            messages.append({"role": norm, "content": text})
    messages = messages[-MAX_HISTORY_TURNS:]

    # tell the model which files came with this message (names only, already cleaned of paths/control chars)
    shown = message + (f"\n\n[Files attached to this message: {', '.join(names)}]" if names else "")
    # the dashboard may already include the new message in the history - do not add it twice
    if messages and messages[-1]["role"] == "user" and messages[-1]["content"] == message.strip():
        messages[-1]["content"] = shown
    else:
        messages.append({"role": "user", "content": shown})

    _turn.set({"message": message, "calls": 0, "seen": {}})
    async with metrics.measure("chat"):
        result = await agent.ainvoke({"messages": messages}, config=metrics.config())
    content = result["messages"][-1].content
    if isinstance(content, list):
        content = "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return content