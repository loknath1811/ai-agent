"""Files attached in the chat: validate, store, link to tasks, and load for the assignment email.

Rules (all limits are settings, see config.py):
- only a leader can upload, and can only attach files they uploaded themselves
- a file must have a name and some content, and must not be a type mail servers reject outright
  (programs and scripts), because one rejected file would make the whole assignment email fail
- file type is judged by its name, never by what the browser claims
"""
import mimetypes
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import List, Optional, Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from ..config import get_settings
from ..models import Attachment, Employee, TaskAttachment
from ..timeutil import utcnow

# Gmail and most company mail servers refuse these, even inside a zip in some cases.
BLOCKED_EXTENSIONS = {
    "exe", "bat", "cmd", "com", "scr", "pif", "msi", "msp", "dll", "cpl", "jar", "js", "jse", "vbs", "vbe",
    "wsf", "wsh", "ps1", "psm1", "lnk", "reg", "hta", "apk", "app", "gadget", "sh",
}


class AttachmentError(ValueError):
    """A problem with an attachment, worded so it can be shown to the leader as-is."""


@dataclass
class FileToSend:
    filename: str
    content_type: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


def clean_filename(name: Optional[str]) -> str:
    """Keep only the file's own name: no folders, no control characters, a sane length."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip().strip(".")
    if len(name) > 120:
        base, dot, ext = name.rpartition(".")
        name = (base[: 120 - len(ext) - 1] + "." + ext) if dot and len(ext) <= 10 else name[:120]
    return name


def guess_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def human_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def check_upload(filename: str, data: bytes) -> None:
    s = get_settings()
    if not filename:
        raise AttachmentError("The file has no name.")
    if not data:
        raise AttachmentError(f"'{filename}' is empty.")
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext in BLOCKED_EXTENSIONS:
        raise AttachmentError(
            f"'{filename}' cannot be attached: .{ext} files are blocked by mail servers. "
            f"Put it in a .zip or share a link instead."
        )
    if len(data) > s.max_attachment_mb * 1024 * 1024:
        raise AttachmentError(f"'{filename}' is {human_size(len(data))}; the limit is {s.max_attachment_mb} MB per file.")


async def purge_orphans(session: AsyncSession) -> int:
    """Delete uploads that were never attached to a task and are older than the grace period."""
    cutoff = utcnow() - timedelta(hours=get_settings().attachment_orphan_hours)
    used = select(TaskAttachment.attachment_id)
    res = await session.execute(delete(Attachment).where(Attachment.created_at < cutoff, Attachment.id.not_in(used)))
    return res.rowcount or 0


async def save_upload(session: AsyncSession, uploader: Employee, filename: Optional[str], data: bytes) -> Attachment:
    name = clean_filename(filename)
    check_upload(name, data)
    await purge_orphans(session)           # housekeeping rides along with uploads; no extra scheduler job
    att = Attachment(filename=name, content_type=guess_type(name), size=len(data), data=data,
                     uploaded_by_id=uploader.id)
    session.add(att)
    await session.commit()
    return att


async def load_for_actor(session: AsyncSession, actor: Employee, ids: Sequence[int]) -> List[Attachment]:
    """The attachments a leader named in the chat, checked: they exist, are the leader's own uploads, and
    fit the limits. Returns them in the order given (metadata only, not the bytes)."""
    s = get_settings()
    ids = list(dict.fromkeys(int(i) for i in ids))          # drop duplicates, keep order
    if not ids:
        return []
    if len(ids) > s.max_attachments:
        raise AttachmentError(f"At most {s.max_attachments} files can go with one task.")
    rows = (await session.execute(select(Attachment).where(Attachment.id.in_(ids)))).scalars().all()
    by_id = {a.id: a for a in rows}
    for i in ids:
        a = by_id.get(i)
        if a is None or a.uploaded_by_id != actor.id:
            raise AttachmentError("One of the attached files was not found (it may have expired). Please attach it again.")
    ordered = [by_id[i] for i in ids]
    total = sum(a.size for a in ordered)
    if total > s.max_attachments_total_mb * 1024 * 1024:
        raise AttachmentError(
            f"The files total {human_size(total)}; the limit for one email is {s.max_attachments_total_mb} MB."
        )
    return ordered


async def link_to_task(session: AsyncSession, task_id: int, attachment_ids: Sequence[int]) -> None:
    """Record that these files go with this task. No commit: the caller commits together with the task, so
    the background sender can never see a task whose files are not linked yet."""
    for aid in dict.fromkeys(attachment_ids):
        session.add(TaskAttachment(task_id=task_id, attachment_id=aid))


async def files_for_task(session: AsyncSession, task_id: int) -> List[FileToSend]:
    """The files (with their bytes) to put on this task's assignment email."""
    res = await session.execute(
        select(Attachment).join(TaskAttachment, TaskAttachment.attachment_id == Attachment.id)
        .where(TaskAttachment.task_id == task_id).options(undefer(Attachment.data)).order_by(Attachment.id)
    )
    return [FileToSend(a.filename, a.content_type, bytes(a.data)) for a in res.scalars()]
