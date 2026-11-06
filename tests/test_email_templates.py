"""The email templates: structure, HTML safety, and that sending uses both parts."""
from types import SimpleNamespace as NS

import pytest

from app import email_client, email_templates
from app.timeutil import utcnow


def _task(title="NCR review", desc=None):
    ravi = NS(full_name="Ravi Kumar", email="ravi@example.com")
    boss = NS(full_name="Anita Rao", email="anita@example.com")
    return NS(id=8, title=title, description=desc, status="pending", due_date=utcnow(),
              followup_count=2, assignee=ravi, assigned_by=boss)


def test_assignment_has_the_full_structure():
    t = _task(desc="Please include the vendor report.")
    text, html = email_templates.assignment(t, t.assigned_by)
    assert text.startswith("Hi Ravi,")
    assert "Anita Rao has assigned you a new task." in text
    assert "#8 - NCR review" in text and "Deadline:" in text
    assert "Please include the vendor report." in text
    assert "What to do next:" in text
    assert "Regards," in text and "Dhruva Space" in text
    assert "<html>" in html and "NCR review" in html


def test_html_escapes_user_text():
    t = _task(title="<script>alert(1)</script>", desc="a < b & <b>bold</b>")
    _, html = email_templates.assignment(t, t.assigned_by)
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "<b>bold</b>" not in html


def test_escalation_and_blocker_are_flagged():
    t = _task()
    boss = t.assigned_by
    text, _ = email_templates.escalation(boss, t, "no reply after 3 reminders")
    assert "*** ESCALATION ***" in text and "Reminders unanswered:" in text
    text, _ = email_templates.blocker(boss, t, "waiting for the vendor part")
    assert "*** BLOCKED ***" in text and "waiting for the vendor part" in text


def test_missing_name_does_not_crash():
    assert email_templates.first_name("") == "there"
    assert email_templates.first_name(None) == "there"


async def test_send_email_is_multipart_when_html_given(monkeypatch):
    seen = {}

    async def fake_smtp(msg, **kw):
        seen["msg"] = msg
    monkeypatch.setattr(email_client.aiosmtplib, "send", fake_smtp)
    await email_client.send_email("a@b.com", "[Task #1] x", "plain body", html="<p>html body</p>")
    msg = seen["msg"]
    assert msg.is_multipart()
    assert "plain body" in msg.get_body(("plain",)).get_content()
    assert "html body" in msg.get_body(("html",)).get_content()


async def test_send_email_without_html_stays_plain(monkeypatch):
    seen = {}

    async def fake_smtp(msg, **kw):
        seen["msg"] = msg
    monkeypatch.setattr(email_client.aiosmtplib, "send", fake_smtp)
    await email_client.send_email("a@b.com", "s", "just text")
    assert not seen["msg"].is_multipart()
