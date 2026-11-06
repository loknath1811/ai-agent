"""Request-level idempotency: never act twice on the SAME request.

Why this exists on top of the title/deadline check in services/tasks.py: that check compares what the AI
PRODUCED, and the AI words things differently every time ("Finish..." vs "Complete...", a different date).
This one compares what the person actually SENT, which never varies:
    chat  -> the leader's own message text
    email -> the email's Message-ID

The pattern (the same one payment systems use):
    claim()    "is this request new?"  Writes a row first. If the row already exists it is a repeat.
    finish()   save what we answered, so a repeat can be given the same answer.
    release()  nothing was created (or it crashed): forget the claim so the person may simply try again.

Safety against two identical requests at the same instant: the row's primary key is (fingerprint + time slot),
so the DATABASE refuses the second insert; the loser is told "repeat".
"""
import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import RequestLog
from ..timeutil import utcnow

KEEP_DAYS = 30            # older rows are deleted; they no longer protect anything


@dataclass
class Claim:
    repeat: bool                     # True = we already handled (or are handling) this exact request
    key: Optional[str] = None        # pass to finish() / release()
    summary: Optional[str] = None    # what we answered the first time (repeats only)
    age_minutes: int = 0             # how long ago the first one was (repeats only)

    def age_text(self) -> str:
        return "a moment ago" if self.age_minutes < 1 else f"{self.age_minutes} min ago"


def normalize_text(text: str) -> str:
    """Ignore case, extra spaces and punctuation, so 'Ask Ravi to do X.' and 'ask  ravi to do x' match."""
    return re.sub(r"[^\w]+", " ", (text or "").casefold()).strip()


def fingerprint(*parts) -> str:
    raw = "|".join(normalize_text(p) if isinstance(p, str) else str(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()


def _slot_key(fp: str, now: datetime, window_minutes: int) -> str:
    slot = int((now - datetime(1970, 1, 1)).total_seconds() // (window_minutes * 60))
    return hashlib.sha256(f"{fp}|{slot}".encode()).hexdigest()


async def claim(session: AsyncSession, fp: str, *, window_minutes: int) -> Claim:
    """Is this request new? New -> Claim(repeat=False, key=...) and the claim is saved right away so
    others can see it. A repeat inside `window_minutes` -> Claim(repeat=True, ...). window 0 = switched off."""
    if window_minutes <= 0:
        return Claim(repeat=False)
    now = utcnow()
    cutoff = now - timedelta(minutes=window_minutes)
    key = _slot_key(fp, now, window_minutes)

    for _ in range(2):
        row = (await session.execute(
            select(RequestLog).where(RequestLog.fingerprint == fp, RequestLog.created_at >= cutoff)
            .order_by(RequestLog.created_at).limit(1)
        )).scalars().first()
        if row is not None:
            return Claim(True, row.key, row.summary, int((now - row.created_at).total_seconds() // 60))
        try:
            await session.execute(delete(RequestLog).where(RequestLog.created_at < now - timedelta(days=KEEP_DAYS)))
            async with session.begin_nested():            # SAVEPOINT: a refused insert undoes only itself
                session.add(RequestLog(key=key, fingerprint=fp, created_at=now))
                await session.flush()
            await session.commit()                         # visible to everyone from now on
            return Claim(repeat=False, key=key)
        except IntegrityError:
            continue                                       # someone claimed it a moment ago: look again, it is a repeat
    return Claim(True, key, None, 0)


async def finish(session: AsyncSession, key: Optional[str], summary: str) -> None:
    """Remember what we answered, so a repeat gets the same answer."""
    if not key:
        return
    await session.execute(update(RequestLog).where(RequestLog.key == key).values(summary=(summary or "")[:2000]))
    await session.commit()


async def release(session: AsyncSession, key: Optional[str]) -> None:
    """Forget the claim (nothing was created, or we crashed) so the same request may be tried again."""
    if not key:
        return
    try:
        await session.rollback()
        await session.execute(delete(RequestLog).where(RequestLog.key == key))
        await session.commit()
    except Exception:
        await session.rollback()
