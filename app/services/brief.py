"""The morning brief: ONE email that tells the top leader what changed, what needs them, and what is heading
for trouble. Everything is counted from the database with fixed rules - no AI - so it is exact and cheap."""
import logging
import re
from datetime import datetime, timedelta
from html import escape
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import email_client
from ..config import get_settings
from ..models import Employee, Task, TaskEvent, TaskStatus
from ..timeutil import fmt_local, to_local, utcnow
from . import reports, risk

log = logging.getLogger(__name__)

BLOCKED_HOURS = 24      # blocked this long -> "needs you today"
LIST_LIMIT = 8          # items shown per list in the email


def _span(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    return f"{hours // 24} day(s)" if hours >= 48 else f"{max(hours, 1)} hour(s)"


async def recipients(session: AsyncSession) -> List[str]:
    """BRIEF_TO if set, otherwise every active leader who has no manager (the top of the org chart)."""
    override = get_settings().brief_to.strip()
    if override:
        return [e.lower() for e in re.split(r"[,;\s]+", override) if "@" in e]
    res = await session.execute(
        select(Employee).where(Employee.active.is_(True), Employee.is_leader.is_(True), Employee.manager_id.is_(None))
        .order_by(Employee.full_name))
    return [e.email for e in res.scalars().unique()]


def _item(t: Task, reason: str) -> dict:
    return {"id": t.id, "title": t.title, "assignee": t.assignee.full_name,
            "team": t.assignee.team.name if t.assignee.team else "", "reason": reason,
            "due": fmt_local(t.due_date)}


async def build_brief(session: AsyncSession, now: Optional[datetime] = None) -> dict:
    s = get_settings()
    now = now or utcnow()
    local = to_local(now)
    monday = s.skip_weekends and local.weekday() == 0
    since = now - timedelta(hours=72 if monday else 24)

    tasks = list((await session.execute(select(Task))).scalars().unique())
    open_tasks = [t for t in tasks if t.status != TaskStatus.DONE.value]
    summary = reports.summarize(tasks, now)

    events = list((await session.execute(
        select(TaskEvent).where(TaskEvent.created_at >= since,
                                TaskEvent.event_type.in_(["created", "status_change", "escalated"]))
    )).scalars())
    finished = {e.task_id for e in events if e.event_type == "status_change" and e.new_status == "done"}
    went_blocked = {e.task_id for e in events if e.event_type == "status_change" and e.new_status == "blocked"}
    escalated = {e.task_id for e in events if e.event_type == "escalated"}
    created = {e.task_id for e in events if e.event_type == "created"}

    # how long has each blocked task been blocked? (from its history)
    blocked_ids = [t.id for t in open_tasks if t.status == TaskStatus.BLOCKED.value]
    blocked_since: Dict[int, datetime] = {}
    if blocked_ids:
        res = await session.execute(select(TaskEvent).where(
            TaskEvent.task_id.in_(blocked_ids), TaskEvent.event_type == "status_change",
            TaskEvent.new_status == "blocked"))
        for e in res.scalars():
            blocked_since[e.task_id] = max(e.created_at, blocked_since.get(e.task_id, e.created_at))

    # NEEDS YOU TODAY: already a problem
    needs: List[Tuple[int, datetime, dict]] = []
    for t in open_tasks:
        reasons: List[str] = []
        if reports.is_overdue(t, now):
            reasons.append(f"overdue by {_span(now - t.due_date)}")
        if t.escalated:
            reasons.append("escalated")
        if t.status == TaskStatus.BLOCKED.value:
            since_b = blocked_since.get(t.id) or t.updated_at or t.created_at or now
            if now - since_b >= timedelta(hours=BLOCKED_HOURS):
                reasons.append(f"blocked for {_span(now - since_b)}")
        if reasons:
            needs.append((-len(reasons), t.due_date or datetime.max, _item(t, "; ".join(reasons))))
    needs.sort(key=lambda x: (x[0], x[1]))
    needs_ids = {n[2]["id"] for n in needs}

    # AT RISK: heading for trouble
    at_risk = sorted(
        ((t, risk.risk_reasons(t, now)) for t in open_tasks if t.id not in needs_ids),
        key=lambda tr: tr[0].due_date or datetime.max)
    at_risk = [(t, r) for t, r in at_risk if r]

    # by division
    per_div: Dict[str, dict] = {}
    for t in open_tasks:
        team = t.assignee.team
        name = team.division.name if team and team.division else "(No division)"
        d = per_div.setdefault(name, {"name": name, "open": 0, "issues": 0})
        d["open"] += 1
        if t.id in needs_ids or t.status == TaskStatus.BLOCKED.value or t.escalated:
            d["issues"] += 1

    b = summary["by_status"]
    return {
        "date_label": local.strftime("%a %d %b %Y"),
        "window_label": "Since Friday" if monday else "Last 24 hours",
        "new_tasks": len(created), "finished": len(finished),
        "new_blockers": len(went_blocked), "escalations": len(escalated),
        "snapshot": {"open": summary["open"], "on_track": b["on-track"], "pending": b["pending"],
                     "blocked": b["blocked"], "delayed": b["delayed"],
                     "overdue": summary["overdue"], "escalated": summary["escalated"]},
        "needs": [n[2] for n in needs[:LIST_LIMIT]], "needs_more": max(0, len(needs) - LIST_LIMIT),
        "at_risk": [_item(t, "; ".join(r)) for t, r in at_risk[:LIST_LIMIT]],
        "at_risk_more": max(0, len(at_risk) - LIST_LIMIT),
        "divisions": sorted(per_div.values(), key=lambda d: (-d["issues"], d["name"])),
    }


# ----------------------------------------------------------------------
# rendering
# ----------------------------------------------------------------------
def _line(it: dict) -> str:
    return f"#{it['id']} {it['title']} - {it['assignee']}{' (' + it['team'] + ')' if it['team'] else ''}: {it['reason']}"


def render_brief(data: dict) -> Tuple[str, str, str]:
    """-> (subject, plain text, html)"""
    s = get_settings()
    snap = data["snapshot"]
    subject = f"Work status brief - {data['date_label']}"
    headline = (f"{data['window_label']}: {data['finished']} finished, {data['new_tasks']} new, "
                f"{data['new_blockers']} newly blocked, {data['escalations']} escalated.")
    now_line = (f"Right now: {snap['open']} open - {snap['on_track']} on track, {snap['pending']} awaiting a reply, "
                f"{snap['blocked']} blocked, {snap['delayed']} delayed; {snap['overdue']} overdue.")

    t = ["Good morning,", "", headline, now_line, "", "NEEDS YOU TODAY"]
    t += [f"  - {_line(i)}" for i in data["needs"]] or ["  Nothing - no overdue, escalated or long-blocked tasks."]
    if data["needs_more"]:
        t.append(f"  ... and {data['needs_more']} more")
    t += ["", "AT RISK (not late yet, but heading there)"]
    t += [f"  - {_line(i)}" for i in data["at_risk"]] or ["  Nothing at risk right now."]
    if data["at_risk_more"]:
        t.append(f"  ... and {data['at_risk_more']} more")
    t += ["", "BY DIVISION"] + [f"  - {d['name']}: {d['open']} open, {d['issues']} with issues" for d in data["divisions"]]
    if s.dashboard_url:
        t += ["", f"Open the dashboard: {s.dashboard_url}"]
    t += ["", "Regards,", s.agent_name, "Dhruva Space"]

    def block(title: str, colour: str, items: List[dict], more: int, empty: str) -> str:
        rows = "".join(
            f'<tr><td style="padding:6px 0;border-bottom:1px solid #e3e8f0"><b>#{i["id"]} {escape(i["title"])}</b><br>'
            f'<span style="color:#5b6b82">{escape(i["assignee"])}{" (" + escape(i["team"]) + ")" if i["team"] else ""}'
            f' &middot; due {escape(i["due"])}</span><br><span style="color:{colour};font-weight:600">{escape(i["reason"])}</span></td></tr>'
            for i in items)
        body = f'<table role="presentation" style="width:100%;border-collapse:collapse">{rows}</table>' if rows else \
            f'<p style="margin:0;color:#5b6b82">{escape(empty)}</p>'
        extra = f'<p style="margin:6px 0 0;color:#5b6b82">... and {more} more</p>' if more else ""
        return (f'<h3 style="margin:22px 0 8px;font-size:14px;letter-spacing:1px;color:{colour}">{escape(title)}</h3>'
                f"{body}{extra}")

    divs = "".join(
        f'<tr><td style="padding:4px 0">{escape(d["name"])}</td><td style="padding:4px 0;text-align:right">'
        f'{d["open"]} open &middot; <b style="color:{"#d93b3b" if d["issues"] else "#1f9d68"}">{d["issues"]} with issues</b></td></tr>'
        for d in data["divisions"])
    link = (f'<p style="margin:20px 0 0"><a href="{escape(s.dashboard_url)}" style="color:#1f5eff">Open the dashboard</a></p>'
            if s.dashboard_url else "")
    html = f"""<!DOCTYPE html>
<html><body style="margin:0;padding:24px 0;background:#eef1f6;font-family:Segoe UI,Arial,sans-serif;font-size:15px;line-height:1.5;color:#0f1b2d">
<table role="presentation" align="center" style="width:100%;max-width:640px;background:#ffffff;border-radius:6px;border-collapse:collapse;overflow:hidden">
<tr><td style="background:#0f1b2d;color:#ffffff;padding:14px 24px;font-size:16px;font-weight:700;letter-spacing:2px">DHRUVA SPACE</td></tr>
<tr><td style="padding:24px">
<p style="margin:0 0 4px;font-size:18px;font-weight:700">Work status brief</p>
<p style="margin:0 0 14px;color:#5b6b82">{escape(data['date_label'])}</p>
<p style="margin:0 0 6px">{escape(headline)}</p>
<p style="margin:0">{escape(now_line)}</p>
{block("NEEDS YOU TODAY", "#d93b3b", data["needs"], data["needs_more"], "Nothing - no overdue, escalated or long-blocked tasks.")}
{block("AT RISK (not late yet, but heading there)", "#b7791f", data["at_risk"], data["at_risk_more"], "Nothing at risk right now.")}
<h3 style="margin:22px 0 8px;font-size:14px;letter-spacing:1px">BY DIVISION</h3>
<table role="presentation" style="width:100%;border-collapse:collapse">{divs}</table>
{link}
<p style="margin:24px 0 0">Regards,<br><b>{escape(s.agent_name)}</b><br>Dhruva Space</p>
</td></tr>
<tr><td style="padding:12px 24px;background:#f4f6fa;color:#5b6b82;font-size:12px">This is an automated message from the Dhruva Space work-tracking agent.</td></tr>
</table></body></html>"""
    return subject, "\n".join(t), html


async def preview(session: AsyncSession) -> dict:
    data = await build_brief(session)
    subject, text, html = render_brief(data)
    return {"recipients": await recipients(session), "subject": subject, "text": text, "html": html}


async def send_brief(session: AsyncSession) -> dict:
    """Build the brief and email it to every recipient. Each address succeeds or fails on its own."""
    to = await recipients(session)
    if not to:
        return {"results": [], "message": "Nobody to send to: no active leader without a manager, and BRIEF_TO is empty."}
    subject, text, html = render_brief(await build_brief(session))
    results = []
    for addr in to:
        try:
            await email_client.send_email(to=addr, subject=subject, body=text, html=html)
            results.append({"to": addr, "ok": True, "error": None})
        except Exception as e:
            log.error("could not send the brief to %s: %s", addr, e)
            results.append({"to": addr, "ok": False, "error": f"{type(e).__name__}: {e}"[:200]})
    return {"results": results, "message": None}
