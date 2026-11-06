"""Every email the agent sends is built HERE, so they all share one structure:

    Greeting           Hi Ravi,
    Opening line       one sentence: why you are getting this email
    Details block      Task / Deadline / Status ... as label: value rows
    Note (optional)    longer text, e.g. the leader's instructions
    What to do next    one clear call to action
    Sign-off           Regards, <agent name>, Dhruva Space
    Footer             "automated message" line

Each builder returns (plain_text, html). The mail client sends BOTH in one
message; the person's mail app shows the HTML, and text-only apps show the text.

SUBJECT LINES ARE NOT BUILT HERE on purpose: replies are matched to tasks using
"[Task #N]" in the subject (see services/inbox.match_task). Do not change them
without changing that matching code.
"""
from dataclasses import dataclass, field
from html import escape
from typing import List, Optional, Sequence, Tuple

from .config import get_settings
from .timeutil import fmt_local

# colours used by the HTML version, per kind of message
_TONES = {
    "info": "#1f5eff",
    "warning": "#d93b3b",
    "success": "#1f9d68",
}


@dataclass
class EmailContent:
    first_name: str
    intro: str
    details: List[Tuple[str, str]] = field(default_factory=list)   # label, value rows
    note: Optional[Tuple[str, str]] = None                          # heading, text
    action: Optional[Tuple[str, str]] = None                        # heading, text
    banner: Optional[str] = None                                    # e.g. "ESCALATION"
    tone: str = "info"                                              # info | warning | success


# ----------------------------------------------------------------------
# the two renderers
# ----------------------------------------------------------------------
def _signature_lines() -> List[str]:
    return ["Regards,", get_settings().agent_name, "Dhruva Space"]


def render_text(c: EmailContent) -> str:
    out: List[str] = []
    if c.banner:
        out += [f"*** {c.banner} ***", ""]
    out += [f"Hi {c.first_name},", "", c.intro]

    if c.details:
        width = max(len(label) for label, _ in c.details) + 2
        out.append("")
        out += [f"  {(label + ':').ljust(width)}{value}" for label, value in c.details]

    if c.note:
        heading, text = c.note
        out += ["", f"{heading}:", text]

    if c.action:
        heading, text = c.action
        out += ["", f"{heading}:", text]

    out += [""] + _signature_lines()
    out += ["", "--", "This is an automated message from the Dhruva Space work-tracking agent."]
    return "\n".join(out)


def _p(text: str) -> str:
    """Escape user-supplied text and keep its line breaks. Never put raw text in HTML."""
    return escape(text).replace("\n", "<br>")


def render_html(c: EmailContent) -> str:
    colour = _TONES.get(c.tone, _TONES["info"])
    s = get_settings()
    rows = "".join(
        f'<tr><td style="padding:6px 12px 6px 0;color:#5b6b82;vertical-align:top;white-space:nowrap">{_p(label)}</td>'
        f'<td style="padding:6px 0;color:#0f1b2d;font-weight:600">{_p(value)}</td></tr>'
        for label, value in c.details
    )
    details = (
        f'<table role="presentation" style="margin:16px 0;padding:12px 16px;width:100%;'
        f'background:#f4f6fa;border-left:4px solid {colour};border-collapse:separate">{rows}</table>'
        if rows else ""
    )
    note = (
        f'<p style="margin:16px 0 4px;font-weight:600">{_p(c.note[0])}</p><p style="margin:0">{_p(c.note[1])}</p>'
        if c.note else ""
    )
    action = (
        f'<p style="margin:16px 0 4px;font-weight:600">{_p(c.action[0])}</p><p style="margin:0">{_p(c.action[1])}</p>'
        if c.action else ""
    )
    banner = (
        f'<div style="background:{colour};color:#fff;padding:6px 24px;font-size:12px;font-weight:700;'
        f'letter-spacing:1px">{_p(c.banner)}</div>' if c.banner else ""
    )
    return f"""<!DOCTYPE html>
<html><body style="margin:0;padding:24px 0;background:#eef1f6;font-family:Segoe UI,Arial,sans-serif;font-size:15px;line-height:1.5;color:#0f1b2d">
<table role="presentation" align="center" style="width:100%;max-width:600px;background:#ffffff;border-radius:6px;border-collapse:collapse;overflow:hidden">
<tr><td style="background:#0f1b2d;color:#ffffff;padding:14px 24px;font-size:16px;font-weight:700;letter-spacing:2px">DHRUVA SPACE</td></tr>
<tr><td>{banner}</td></tr>
<tr><td style="padding:24px">
<p style="margin:0 0 12px">Hi {_p(c.first_name)},</p>
<p style="margin:0">{_p(c.intro)}</p>
{details}{note}{action}
<p style="margin:24px 0 0">Regards,<br><b>{_p(s.agent_name)}</b><br>Dhruva Space</p>
</td></tr>
<tr><td style="padding:12px 24px;background:#f4f6fa;color:#5b6b82;font-size:12px">This is an automated message from the Dhruva Space work-tracking agent.</td></tr>
</table></body></html>"""


def render(c: EmailContent) -> Tuple[str, str]:
    return render_text(c), render_html(c)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def first_name(full_name: Optional[str]) -> str:
    parts = (full_name or "").split()
    return parts[0] if parts else "there"


def _task_details(task, *, with_assignee: bool = False, with_reminders: bool = False) -> List[Tuple[str, str]]:
    rows = [("Task", f"#{task.id} - {task.title}")]
    if with_assignee:
        rows.append(("Assigned to", f"{task.assignee.full_name} ({task.assignee.email})"))
    rows += [("Deadline", fmt_local(task.due_date)), ("Status", task.status)]
    if with_reminders:
        rows.append(("Reminders unanswered", str(task.followup_count)))
    return rows


_HOW_TO_REPLY = (
    "Reply to this email with a short update, for example: on track, "
    "blocked (and why), delayed, or done."
)


# ----------------------------------------------------------------------
# one builder per kind of email -> (plain_text, html)
# ----------------------------------------------------------------------
def _size(n: int) -> str:
    return f"{n} B" if n < 1024 else f"{n / 1024:.0f} KB" if n < 1024 * 1024 else f"{n / (1024 * 1024):.1f} MB"


def assignment(task, assigner, attachments: Optional[Sequence[Tuple[str, int]]] = None) -> Tuple[str, str]:
    """`attachments` = (file name, size in bytes) of the files that travel with this very email."""
    who = assigner.full_name if assigner else "Management"
    rows = [("Task", f"#{task.id} - {task.title}"), ("Assigned by", who),
            ("Deadline", fmt_local(task.due_date))]
    if attachments:
        rows.append(("Attached", "; ".join(f"{n} ({_size(sz)})" for n, sz in attachments)))
    return render(EmailContent(
        first_name=first_name(task.assignee.full_name),
        intro=f"{who} has assigned you a new task.",
        details=rows,
        note=("Details", task.description) if task.description else None,
        action=("What to do next",
                _HOW_TO_REPLY + " I will follow up if I do not hear from you."),
    ))


def reminder(task) -> Tuple[str, str]:
    return render(EmailContent(
        first_name=first_name(task.assignee.full_name),
        intro="This is a quick check-in on the task below. I have not received an update recently.",
        details=_task_details(task),
        action=("What to do next", _HOW_TO_REPLY),
    ))


def escalation(person, task, reason: str) -> Tuple[str, str]:
    return render(EmailContent(
        first_name=first_name(person.full_name),
        intro=f"This task needs attention: {reason}",
        details=_task_details(task, with_assignee=True, with_reminders=True),
        action=("What to do next",
                "Please follow up with the assignee directly. The agent has stopped sending reminders "
                "for this task; it will resume when the assignee replies."),
        banner="ESCALATION", tone="warning",
    ))


def blocker(person, task, what: str) -> Tuple[str, str]:
    return render(EmailContent(
        first_name=first_name(person.full_name),
        intro=f"{task.assignee.full_name} has reported a blocker on the task below.",
        details=_task_details(task, with_assignee=True),
        note=("Blocker reported", what),
        action=("What to do next", "Please help remove this blocker or contact the assignee."),
        banner="BLOCKED", tone="warning",
    ))


def done_notice(person, task, summary: Optional[str]) -> Tuple[str, str]:
    return render(EmailContent(
        first_name=first_name(person.full_name),
        intro=f"{task.assignee.full_name} has reported the task below as done.",
        details=_task_details(task, with_assignee=True),
        note=("Summary", summary) if summary else None,
        banner="DONE", tone="success",
    ))


def acknowledgement(sender, task, status: str, summary: Optional[str]) -> Tuple[str, str]:
    return render(EmailContent(
        first_name=first_name(sender.full_name),
        intro=f"Thank you for your update. I have recorded your status as '{status}'.",
        details=[("Task", f"#{task.id} - {task.title}"), ("Recorded status", status)],
        note=("Your update, as I understood it", summary) if summary else None,
        tone="success",
    ))


def status_check(sender, task, status: str, summary: Optional[str]) -> Tuple[str, str]:
    """Asked when the AI was not sure it read the reply correctly."""
    return render(EmailContent(
        first_name=first_name(sender.full_name),
        intro="Thank you for your update. I want to make sure I understood it correctly.",
        details=[("Task", f"#{task.id} - {task.title}"), ("I understood your status as", status)],
        note=("Your message, as I understood it", summary) if summary else None,
        action=("Is that right?",
                "Reply 'yes' if it is. If not, just tell me the correct status "
                "(for example: done, blocked, delayed, or on track)."),
        tone="warning",
    ))


def status_correction_notice(sender, task, old: str, new: str, by_name: str) -> Tuple[str, str]:
    """Tells the assignee that someone changed the recorded status of their task."""
    return render(EmailContent(
        first_name=first_name(sender.full_name),
        intro=f"{by_name} changed the recorded status of your task.",
        details=[("Task", f"#{task.id} - {task.title}"), ("Was", old), ("Now", new)],
        action=("If this is not right", "Just reply to this email and tell me the correct status."),
    ))


def which_task(sender, open_tasks: Sequence) -> Tuple[str, str]:
    listing = "\n".join(f"#{t.id} - {t.title} (deadline {fmt_local(t.due_date)})" for t in open_tasks)
    return render(EmailContent(
        first_name=first_name(sender.full_name),
        intro="You have several open tasks, and I could not tell which one your message is about.",
        note=("Your open tasks", listing),
        action=("What to do next",
                f"Please reply to the original task email, or mention the task number "
                f"(for example 'Task #{open_tasks[0].id}')."),
    ))


def assignment_result(leader, result_text: str) -> Tuple[str, str]:
    return render(EmailContent(
        first_name=first_name(leader.full_name),
        intro="Here is the result of your request.",
        note=("Result", result_text),
    ))
