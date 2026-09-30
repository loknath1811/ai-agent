# app/email_client.py
import os
import ssl
import base64
from email.message import EmailMessage
from typing import List, Tuple

import aiosmtplib
import imapclient
import anyio
from dotenv import load_dotenv

load_dotenv()  # pull credentials from .env

SMTP_USER = os.getenv("GMAIL_SMTP_USER")
SMTP_PASS = os.getenv("GMAIL_SMTP_PASSWORD")
IMAP_USER = os.getenv("GMAIL_IMAP_USER")
IMAP_PASS = os.getenv("GMAIL_IMAP_PASSWORD")
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993


async def send_email(to: str, subject: str, body: str) -> None:
    """Send a plain‑text email via Gmail SMTP.

    Raises:
        aiosmtplib.SMTPException on failure.
    """
    message = EmailMessage()
    message["From"] = SMTP_USER
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    # Gmail requires STARTTLS on port 587
    await aiosmtplib.send(
        message,
        hostname=SMTP_HOST,
        port=SMTP_PORT,
        start_tls=True,
        username=SMTP_USER,
        password=SMTP_PASS,
    )


def _decode_bytes(data: bytes) -> str:
    """Best‑effort decode of raw email bytes to UTF‑8 string."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


async def fetch_unread(limit: int = 10) -> List[Tuple[int, str, str]]:
    """Return a list of up to `limit` unread messages.

    Each tuple is ``(uid, subject, plain_body)``.
    """
    def _sync_fetch() -> List[Tuple[int, str, str]]:
        with imapclient.IMAPClient(IMAP_HOST, port=IMAP_PORT, ssl=True) as client:
            client.login(IMAP_USER, IMAP_PASS)
            client.select_folder("INBOX")
            uids = client.search(["UNSEEN"])
            # Restrict to the newest `limit` messages
            uids = sorted(uids, reverse=True)[:limit]

            results: List[Tuple[int, str, str]] = []
            for uid in uids:
                raw = client.fetch([uid], ["RFC822"])[uid][b"RFC822"]
                # Very simple parsing – we only need Subject + plain text body
                from email.parser import BytesParser
                from email.policy import default

                msg = BytesParser(policy=default).parsebytes(raw)
                subject = msg["Subject"] or "(no subject)"
                # Prefer plain‑text part; fallback to the whole payload
                if msg.is_multipart():
                    # Walk parts to find the first text/plain
                    body = ""
                    for part in msg.iter_parts():
                        if part.get_content_type() == "text/plain":
                            body = _decode_bytes(part.get_payload(decode=True))
                            break
                else:
                    body = _decode_bytes(msg.get_payload(decode=True) or b"")

                results.append((uid, subject, body))
            return results

    # Run the blocking IMAP client in a thread so the FastAPI event loop stays responsive
    return await anyio.to_thread.run_sync(_sync_fetch)


# ----------------------------------------------------------------------
# Small sanity‑check helper (you can run it manually)
# ----------------------------------------------------------------------
if __name__ == "__main__":
    import asyncio

    async def _demo():
        print("Sending a test email to yourself …")
        await send_email(
            to=SMTP_USER,
            subject="🤖 Test email from AI Work‑Delegation Agent",
            body="If you see this, the SMTP helper works!",
        )
        print("Fetching up to 5 unread messages …")
        msgs = await fetch_unread(limit=5)
        for uid, sub, body in msgs:
            print(f"\n--- UID {uid} — {sub} ---\n{body[:200]}{'…' if len(body) > 200 else ''}")

    asyncio.run(_demo())


async def mark_as_read(uid: int) -> None:
    """Mark an email as read using its IMAP UID."""

    def _sync_mark() -> None:
        with imapclient.IMAPClient(
            IMAP_HOST,
            port=IMAP_PORT,
            ssl=True
        ) as client:

            client.login(IMAP_USER, IMAP_PASS)
            client.select_folder("INBOX")

            client.add_flags(
                [uid],
                [b"\\Seen"]
            )

    await anyio.to_thread.run_sync(_sync_mark)