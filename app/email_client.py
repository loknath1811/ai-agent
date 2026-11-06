"""Sending (SMTP) and reading (IMAP) email.

Only this file talks to the mail server. To move from Gmail to a company mail
server, change the HOST/PORT/USER/PASSWORD values in .env.
"""
import asyncio
import html as htmllib
import logging
import re
import time
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import make_msgid, parseaddr, getaddresses
from typing import Iterable, List, Optional, Sequence, Set

import aiosmtplib
from imapclient import IMAPClient

from . import metrics
from .config import get_settings

log = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# data type for an incoming email
# ----------------------------------------------------------------------
@dataclass
class InboundEmail:
    uid: int
    message_id: str
    from_email: str
    from_name: str
    subject: str
    body: str                                   # plain text, quoted history removed
    in_reply_to: str = ""
    references: List[str] = field(default_factory=list)
    recipients: List[str] = field(default_factory=list)
    auto_generated: bool = False
    auth_ok: Optional[bool] = None              # SPF/DKIM pass? (None = unknown)


# ----------------------------------------------------------------------
# text cleanup
# ----------------------------------------------------------------------
_QUOTE_HEADERS = [
    re.compile(r"^\s*On .{5,200}wrote:\s*$", re.I),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.I),
    re.compile(r"^\s*_{5,}\s*$"),
    re.compile(r"^\s*From:\s.+", re.I),  # Outlook style header block
]


def html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", html)
    text = re.sub(r"(?s)<[^>]+>", " ", html)
    return htmllib.unescape(text)


def strip_quoted(text: str) -> str:
    """Keep only what the person newly wrote (drop '>' lines and 'On ... wrote:')."""
    out: List[str] = []
    lines = (text or "").replace("\r", "").split("\n")
    for i, line in enumerate(lines):
        if any(p.match(line) for p in _QUOTE_HEADERS):
            # 'On <date> <name>' can wrap onto two lines; the pattern above handles one.
            break
        if line.lstrip().startswith(">"):
            continue
        out.append(line)
    # collapse blank runs and trim
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()
    return cleaned


def _extract_body(msg) -> str:
    try:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        content = part.get_content() or ""
        if part.get_content_type() == "text/html":
            content = html_to_text(content)
        return content
    except Exception as e:  # broken charset etc.
        log.warning("could not read email body: %s", e)
        return ""


def _is_auto(msg, from_email: str) -> bool:
    auto = (msg.get("Auto-Submitted") or "no").strip().lower()
    prec = (msg.get("Precedence") or "").strip().lower()
    local = from_email.split("@")[0]
    return (
        auto != "no"
        or prec in {"bulk", "junk", "list", "auto_reply"}
        or bool(msg.get("X-Autoreply") or msg.get("X-Autorespond"))
        or local in {"mailer-daemon", "postmaster"}
        or local.startswith(("no-reply", "noreply", "donotreply", "do-not-reply"))
    )


def _auth_ok(msg) -> Optional[bool]:
    results = " ".join(str(h) for h in (msg.get_all("Authentication-Results") or [])).lower()
    if not results:
        return None
    return "dkim=pass" in results or "spf=pass" in results


def parse_raw_email(uid: int, raw: bytes) -> InboundEmail:
    msg = BytesParser(policy=policy.default).parsebytes(raw)
    name, addr = parseaddr(str(msg.get("From", "")))
    addr = addr.lower()
    recipients = [a.lower() for _, a in getaddresses([str(msg.get("To", "")), str(msg.get("Cc", ""))]) if a]
    refs = re.findall(r"<[^>]+>", str(msg.get("References", "")))
    return InboundEmail(
        uid=uid,
        message_id=str(msg.get("Message-ID", "")).strip(),
        from_email=addr,
        from_name=name,
        subject=str(msg.get("Subject", "")).strip(),
        body=strip_quoted(_extract_body(msg)),
        in_reply_to=str(msg.get("In-Reply-To", "")).strip(),
        references=refs,
        recipients=recipients,
        auto_generated=_is_auto(msg, addr),
        auth_ok=_auth_ok(msg),
    )


# ----------------------------------------------------------------------
# SENDING
# ----------------------------------------------------------------------
async def send_email(
    to: str,
    subject: str,
    body: str,
    cc: Optional[Iterable[str]] = None,
    in_reply_to: Optional[str] = None,
    task_id: Optional[int] = None,
    html: Optional[str] = None,
    attachments: Optional[Sequence] = None,
) -> str:
    """Send one email. Returns the Message-ID we assigned to it.

    `body` is always the plain-text version. If `html` is given, the message is
    sent as multipart/alternative: mail apps show the HTML, text-only apps show `body`.
    `attachments` is a list of objects with .filename, .content_type and .data (bytes)."""
    s = get_settings()
    msg = EmailMessage()
    msg["From"] = f"{s.agent_name} <{s.smtp_user}>" if s.smtp_user else s.agent_name
    msg["To"] = to
    if cc:
        msg["Cc"] = ", ".join(sorted(set(cc) - {to}))
    msg["Subject"] = subject
    msg["Auto-Submitted"] = "auto-generated"  # stops other auto-responders looping with us
    domain = s.smtp_user.split("@")[-1] if "@" in s.smtp_user else "agent.local"
    message_id = make_msgid(domain=domain)
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    if s.use_plus_addressing and task_id and "@" in s.smtp_user:
        user, dom = s.smtp_user.split("@", 1)
        msg["Reply-To"] = f"{user}+t{task_id}@{dom}"
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    for f in attachments or []:
        maintype, _, subtype = (f.content_type or "application/octet-stream").partition("/")
        msg.add_attachment(f.data, maintype=maintype or "application", subtype=subtype or "octet-stream",
                           filename=f.filename)

    if s.email_dry_run:
        names = f" attachments={[f.filename for f in attachments]}" if attachments else ""
        log.info("[DRY RUN] email to=%s subject=%r%s\n%s", to, subject, names, body)
        return message_id

    if not (s.smtp_user and s.smtp_password):
        raise RuntimeError("GMAIL_SMTP_USER / GMAIL_SMTP_PASSWORD are not set")

    t0 = time.perf_counter()
    try:
        await aiosmtplib.send(
            msg,
            hostname=s.smtp_host,
            port=s.smtp_port,
            username=s.smtp_user,
            password=s.smtp_password,
            start_tls=True,
            timeout=30,
        )
    finally:
        metrics.record_email(time.perf_counter() - t0)
    return message_id


# ----------------------------------------------------------------------
# READING
# ----------------------------------------------------------------------
def _fetch_unread_sync(skip_uids: Set[int], limit: int) -> List[InboundEmail]:
    s = get_settings()
    if not (s.imap_user and s.imap_password):
        raise RuntimeError("GMAIL_IMAP_USER / GMAIL_IMAP_PASSWORD are not set")
    results: List[InboundEmail] = []
    with IMAPClient(s.imap_host, port=s.imap_port, ssl=True, timeout=30) as client:
        client.login(s.imap_user, s.imap_password)
        client.select_folder("INBOX")
        uids = sorted(client.search(["UNSEEN"]), reverse=True)  # newest first
        # skip what we've already handled; keep looking until `limit` NEW ones,
        # so old unread junk can never starve real replies.
        todo = [u for u in uids if u not in skip_uids][:limit]
        if not todo:
            return []
        # BODY.PEEK[] = read WITHOUT marking the email as read
        data = client.fetch(todo, ["BODY.PEEK[]"])
        for uid in todo:
            raw = None
            for key, value in data.get(uid, {}).items():
                if isinstance(value, (bytes, bytearray)) and key.startswith(b"BODY"):
                    raw = bytes(value)
                    break
            if raw:
                try:
                    results.append(parse_raw_email(uid, raw))
                except Exception as e:
                    log.warning("could not parse email uid=%s: %s", uid, e)
    return results


async def fetch_unread(skip_uids: Optional[Set[int]] = None, limit: Optional[int] = None) -> List[InboundEmail]:
    limit = limit or get_settings().imap_fetch_limit
    return await asyncio.to_thread(_fetch_unread_sync, skip_uids or set(), limit)


def _mark_read_sync(uids: List[int]) -> None:
    s = get_settings()
    with IMAPClient(s.imap_host, port=s.imap_port, ssl=True, timeout=30) as client:
        client.login(s.imap_user, s.imap_password)
        client.select_folder("INBOX")
        client.add_flags(uids, [b"\\Seen"])


async def mark_as_read(uids: List[int]) -> None:
    if uids and not get_settings().email_dry_run:
        await asyncio.to_thread(_mark_read_sync, uids)
