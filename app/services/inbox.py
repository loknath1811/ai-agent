"""Reads the agent's mailbox and decides what each email is.

An incoming email can be:
  * a reply about a task            -> classify with the AI, update status
  * a leader giving new instructions -> create tasks
  * something else / unknown sender -> ignored
We match replies to tasks WITHOUT relying on the employee typing "Task #8":
thread headers -> "Task #N" in subject -> plus-address -> the sender's only open
task -> AI picks among their open tasks -> otherwise we ask.
"""
import logging
import re
from dataclasses import dataclass
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import directory, email_client, email_templates, metrics
from ..config import get_settings
from ..database import async_session
from ..email_client import InboundEmail
from ..llm_tasks import LLMError, classify_reply, parse_assignment, pick_task
from ..models import Employee, EmailLog, OutboundEmail, Task
from ..timeutil import fmt_local, parse_due, utcnow
from . import escalation, idempotency
from . import tasks as task_service
from .assign import assign_from_items

log = logging.getLogger(__name__)

# An email's Message-ID is never legitimately sent twice, so remember it for a week.
EMAIL_REPEAT_WINDOW_MINUTES = 7 * 24 * 60

TASK_REF = re.compile(r"(?:task\s*#|#)\s*(\d+)", re.I)
PLUS_REF = re.compile(r"\+t(\d+)@", re.I)


@dataclass
class Outcome:
    status: str          # processed | ignored | failed
    detail: str = ""


# ----------------------------------------------------------------------
# which task is this email about?
# ----------------------------------------------------------------------
async def match_task(session: AsyncSession, em: InboundEmail) -> Optional[Task]:
    # 1) email thread: the reply points at one of OUR outgoing messages
    ids = [i for i in [em.in_reply_to, *em.references] if i]
    if ids:
        res = await session.execute(select(OutboundEmail.task_id).where(OutboundEmail.message_id.in_(ids)))
        row = res.first()
        if row:
            return await task_service.get_task(session, row[0])
    # 2) "Task #8" / "#8" in the subject (survives a normal Reply)
    m = TASK_REF.search(em.subject or "")
    if m:
        task = await task_service.get_task(session, int(m.group(1)))
        if task:
            return task
    # 3) plus-address token  agent+t8@...
    for rcpt in em.recipients:
        m = PLUS_REF.search(rcpt)
        if m:
            task = await task_service.get_task(session, int(m.group(1)))
            if task:
                return task
    return None


# ----------------------------------------------------------------------
# a reply about a known task
# ----------------------------------------------------------------------
# Words that, on their own, mean "yes, you read my status correctly".
_YES_FIRST = {"yes", "yeah", "yep", "yup", "correct", "right", "confirmed", "ok", "okay"}
_NOT_A_PLAIN_YES = {"but", "however", "not", "no", "except", "late", "delay", "delayed", "blocked",
                    "stuck", "done", "finished", "completed", "waiting", "slip", "problem"}


def is_confirmation(body: str) -> bool:
    """True for a short, plain 'yes / that's right' reply. Anything longer or qualified ('yes but it
    will be late') goes to the AI like any other reply, so we never mistake a correction for a yes."""
    first_line = next((ln for ln in (body or "").splitlines() if ln.strip()), "")
    words = re.sub(r"[^a-z0-9 ]+", " ", first_line.lower().replace("\u2019", "'").replace("'", "")).split()
    if not words or len(words) > 4 or words[0] not in _YES_FIRST:
        return False
    return not _NOT_A_PLAIN_YES.intersection(words)


async def _after_status(session: AsyncSession, task: Task, sender: Employee, status: str,
                        blocker_or_summary: Optional[str], summary: Optional[str]) -> None:
    """Emails that follow a status becoming final: alert managers about a blocker, tell the assigner it is
    done, and thank the employee."""
    if status == "blocked":
        await escalation.notify_blocker(session, task, blocker_or_summary)
    elif status == "done":
        await escalation.notify_done(session, task, summary)
    if get_settings().send_acks:
        text, html = email_templates.acknowledgement(sender, task, status, summary)
        await task_service.send_task_email(
            session, task, to=sender, subject=f"Re: [Task #{task.id}] {task.title}",
            body=text, html=html, kind="ack",
        )


async def _confirm(session: AsyncSession, task: Task, sender: Employee) -> Outcome:
    """The employee answered 'yes' to our question: the provisional status is now final."""
    task_id, status = task.id, task.status
    await task_service.settle_status_check(session, task, "status_confirmed",
                                           f"assignee confirmed '{status}'", sender.id)
    await task_service.register_reply(session, task, actor_id=sender.id)
    held = await task_service.last_status_detail(session, task_id)
    await session.commit()
    try:
        await _after_status(session, task, sender, status, held, held)     # alerts that were held back
        await session.commit()
    except Exception as e:
        await session.rollback()
        log.error("side effects failed for task %s: %s", task_id, e)
    return Outcome("processed", f"task {task_id}: status confirmed ({status})")


async def handle_task_reply(session: AsyncSession, em: InboundEmail, sender: Employee, task: Task) -> Outcome:
    s = get_settings()

    if sender.id != task.assignee_id:
        # a manager / assigner commenting - recorded, but only the assignee changes status
        await task_service.log_event(
            session, task.id, "comment", actor_id=sender.id,
            detail=f"{sender.full_name}: {em.body[:300]}",
        )
        await session.commit()
        return Outcome("processed", f"comment on task {task.id} from non-assignee {sender.email}")

    awaiting = task.status_check == "unconfirmed"      # we asked this person to confirm a status
    if awaiting and is_confirmation(em.body):
        return await _confirm(session, task, sender)   # no AI needed for a plain "yes"

    analysis = await classify_reply(task.title, fmt_local(task.due_date), em.body)  # may raise LLMError

    if analysis.status == "none":
        # The reply has no usable status (e.g. just "ok"), but the assignee DID answer, so
        # they must not be chased or escalated for silence. Status is left unchanged.
        await task_service.log_event(session, task.id, "comment", actor_id=sender.id, detail=em.body[:300])
        await task_service.register_reply(session, task, actor_id=sender.id)
        await session.commit()
        return Outcome("processed", f"task {task.id}: reply had no status")

    new_due = parse_due(analysis.new_due_date)
    if new_due and new_due <= utcnow():
        new_due = None  # ignore dates in the past

    detail = analysis.summary or analysis.blocker
    was = task.status
    changed = await task_service.apply_status(
        session, task, analysis.status, actor_id=sender.id, detail=detail, new_due=new_due,
    )
    # Was the AI unsure? Then keep the status, mark it unconfirmed, and ask - unless we are already
    # waiting for an answer, in which case this reply settles the question either way.
    ask = (not awaiting) and s.confirm_low_confidence and analysis.confidence == "low"
    if awaiting:
        kind = "status_confirmed" if analysis.status == was else "status_corrected"
        await task_service.settle_status_check(session, task, kind, f"{was} -> {analysis.status} (assignee's reply)", sender.id)
    elif ask:
        task.status_check = "unconfirmed"
        await task_service.log_event(session, task.id, "status_check_asked", actor_id=sender.id,
                                     detail=f"AI read '{analysis.status}' with low confidence: {detail or ''}"[:300])
    await session.commit()

    # side effects: failures here must not undo the saved status
    task_id = task.id  # read now: attributes expire after a rollback
    try:
        if ask:
            # Alerts to managers are HELD until the employee confirms, so a misread never raises a false alarm.
            text, html = email_templates.status_check(sender, task, analysis.status, analysis.summary)
            await task_service.send_task_email(
                session, task, to=sender, subject=f"Re: [Task #{task.id}] {task.title}",
                body=text, html=html, kind="check",
            )
        else:
            await _after_status(session, task, sender, analysis.status,
                                analysis.blocker or analysis.summary, analysis.summary)
        await session.commit()
    except Exception as e:
        await session.rollback()
        log.error("side effects failed for task %s: %s", task_id, e)

    return Outcome("processed", f"task {task_id}: {analysis.status}" + (" (changed)" if changed else "")
                   + (" - asked to confirm" if ask else ""))


# ----------------------------------------------------------------------
# one email
# ----------------------------------------------------------------------
async def handle_email(session: AsyncSession, em: InboundEmail) -> Outcome:
    s = get_settings()

    if s.smtp_user and em.from_email == s.smtp_user.lower():
        return Outcome("ignored", "sent by the agent mailbox itself")
    if em.auto_generated:
        return Outcome("ignored", "automatic message (out-of-office, bounce, newsletter)")

    if em.message_id:  # same message seen twice (copies in several folders)
        dup = await session.execute(
            select(EmailLog.id).where(EmailLog.message_id == em.message_id, EmailLog.status != "failed")
        )
        if dup.first():
            return Outcome("ignored", "duplicate message-id")

    sender = await directory.get_by_email(session, em.from_email)
    if not sender or not sender.active:
        return Outcome("ignored", f"sender {em.from_email} is not an active employee")
    if not (em.body or "").strip() and not (em.subject or "").strip():
        return Outcome("ignored", "empty email")

    # ---- a reply to a task we know about
    task = await match_task(session, em)
    if task:
        return await handle_task_reply(session, em, sender, task)

    # ---- a leader giving new instructions
    if sender.is_leader:
        allowed = (not s.require_email_auth) or em.auth_ok is True
        if allowed:
            # The same email read twice (e.g. the app restarted before it noted the email as handled) must not
            # create tasks twice. The Message-ID never changes, unlike the AI's wording of the title and date.
            claim = idempotency.Claim(repeat=False)
            if em.message_id and s.duplicate_window_minutes > 0:
                claim = await idempotency.claim(session, idempotency.fingerprint("email", em.message_id),
                                                window_minutes=EMAIL_REPEAT_WINDOW_MINUTES)
                if claim.repeat:
                    return Outcome("processed", f"already handled this email {claim.age_text()}; nothing created again")
            result = None
            try:
                parsed = await parse_assignment(f"{em.subject}\n\n{em.body}")
                if parsed.is_assignment and parsed.items:
                    result = await assign_from_items(session, sender, parsed.items)
                    await session.commit()
            except BaseException:
                await idempotency.release(session, claim.key)       # failed before finishing: allow a retry
                raise
            if result is not None and (result.created or result.duplicates):
                await idempotency.finish(session, claim.key, result.summary())
            else:
                await idempotency.release(session, claim.key)       # nothing created: not a repeat next time
            if result is not None:
                text, html = email_templates.assignment_result(sender, result.summary())
                await email_client.send_email(
                    to=sender.email,
                    subject=f"Re: {em.subject}" if em.subject else "Your assignment request",
                    body=text, html=html,
                    in_reply_to=em.message_id or None,
                )
                return Outcome("processed", f"leader assignment: {len(result.created)} created, {len(result.problems)} problems")
        else:
            log.warning("leader email from %s failed SPF/DKIM check; not treated as instruction", sender.email)

    # ---- an update without a task number: work out which task
    open_tasks = await task_service.open_tasks_for(session, sender.id)
    if not open_tasks:
        return Outcome("ignored", "no task reference and sender has no open tasks")
    if len(open_tasks) == 1:
        return await handle_task_reply(session, em, sender, open_tasks[0])

    picked = await pick_task(em.body, [(t.id, t.title) for t in open_tasks])
    if picked:
        return await handle_task_reply(session, em, sender, next(t for t in open_tasks if t.id == picked))

    text, html = email_templates.which_task(sender, open_tasks)
    await email_client.send_email(
        to=sender.email,
        subject=f"Re: {em.subject}" if em.subject else "Which task is this about?",
        body=text, html=html,
        in_reply_to=em.message_id or None,
    )
    return Outcome("processed", "asked sender which task")


# ----------------------------------------------------------------------
# the whole mailbox
# ----------------------------------------------------------------------
async def _handle_one(em: InboundEmail) -> Outcome:
    async with metrics.measure("email_inbound") as m, async_session() as session:
        try:
            outcome = await handle_email(session, em)
        except LLMError as e:
            await session.rollback()
            outcome = Outcome("failed", f"AI problem: {e}"[:300])
        except Exception as e:
            await session.rollback()
            log.exception("failed to handle email uid=%s", em.uid)
            outcome = Outcome("failed", f"{type(e).__name__}: {e}"[:300])
        m.operation = f"email:{outcome.status}"
        m.note = f"{outcome.status}: {outcome.detail}"

    async with async_session() as session:
        prev = None
        if outcome.status == "failed":
            res = await session.execute(select(EmailLog).where(EmailLog.uid == em.uid, EmailLog.status == "failed"))
            prev = res.scalars().first()
        if prev:
            prev.attempts += 1
            prev.detail = outcome.detail
        else:
            session.add(EmailLog(
                uid=em.uid, message_id=em.message_id or None, from_email=em.from_email,
                subject=(em.subject or "")[:200], status=outcome.status, detail=outcome.detail,
            ))
        await session.commit()
    return outcome


async def process_inbox(limit: Optional[int] = None) -> dict:
    s = get_settings()
    async with async_session() as session:
        res = await session.execute(select(EmailLog.uid, EmailLog.status, EmailLog.attempts))
        skip = {
            uid for uid, status, attempts in res.all()
            if uid is not None and (status in ("processed", "ignored") or attempts >= s.imap_max_attempts)
        }

    emails: List[InboundEmail] = await email_client.fetch_unread(skip_uids=skip, limit=limit or s.imap_fetch_limit)

    stats = {"fetched": len(emails), "processed": 0, "ignored": 0, "failed": 0}
    mark: List[int] = []
    for em in emails:
        outcome = await _handle_one(em)
        stats[outcome.status] += 1
        log.info("email uid=%s from=%s -> %s: %s", em.uid, em.from_email, outcome.status, outcome.detail)
        if outcome.status == "processed" or (outcome.status == "ignored" and s.mark_ignored_as_read):
            mark.append(em.uid)
    try:
        await email_client.mark_as_read(mark)
    except Exception as e:
        log.warning("could not mark emails as read: %s", e)
    return stats
