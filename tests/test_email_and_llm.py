import pytest

from app import llm_tasks
from app.database import build_async_url
from app.email_client import html_to_text, parse_raw_email, strip_quoted


def test_strip_quoted_variants():
    gmail = "Done.\n\nOn Tue, 29 Sep 2026 at 10:00, Agent <a@b.com> wrote:\n> Hi\n> reply please"
    assert strip_quoted(gmail) == "Done."
    outlook = "Blocked on vendor.\n\n-----Original Message-----\nFrom: x\nSent: y\nold text"
    assert strip_quoted(outlook) == "Blocked on vendor."
    assert strip_quoted("> only quoted\n> lines") == ""
    assert strip_quoted("line one\nline two") == "line one\nline two"


def test_parse_plain_email():
    raw = (b"From: Ravi <Ravi.Kumar@Example.com>\r\nTo: agent@x.com\r\nSubject: Re: [Task #5] x\r\n"
           b"Message-ID: <abc@x>\r\nIn-Reply-To: <orig@x>\r\nReferences: <a@x> <orig@x>\r\n"
           b"Content-Type: text/plain; charset=utf-8\r\n\r\nAll done\r\n\r\n> old quoted\r\n")
    m = parse_raw_email(9, raw)
    assert m.from_email == "ravi.kumar@example.com" and m.body == "All done"
    assert m.in_reply_to == "<orig@x>" and m.references == ["<a@x>", "<orig@x>"]
    assert m.auto_generated is False and m.uid == 9


def test_parse_html_only_and_auto_headers():
    raw = (b"From: noreply@vendor.com\r\nSubject: Out of office\r\nAuto-Submitted: auto-replied\r\n"
           b"Content-Type: text/html; charset=utf-8\r\n\r\n<html><body><p>I am away</p><br>back Monday</body></html>")
    m = parse_raw_email(1, raw)
    assert m.auto_generated is True
    assert "I am away" in m.body and "<p>" not in m.body


def test_html_to_text():
    assert "Hello" in html_to_text("<div>Hello&nbsp;world</div><style>x{}</style>")


def test_extract_json_handles_fences_and_noise():
    assert llm_tasks.extract_json('Sure!\n```json\n{"status": "done"}\n```') == {"status": "done"}
    with pytest.raises(llm_tasks.LLMError):
        llm_tasks.extract_json("no json here")


async def test_ask_json_retries_once_on_bad_reply(monkeypatch):
    replies = iter(["I think it is done", '{"status": "done", "summary": "ok"}'])

    async def fake(system, user):
        return next(replies)
    monkeypatch.setattr(llm_tasks, "_invoke", fake)
    r = await llm_tasks.classify_reply("t", None, "all finished")
    assert r.status == "done"


async def test_classify_rejects_invalid_status(monkeypatch):
    async def fake(system, user):
        return '{"status": "banana"}'
    monkeypatch.setattr(llm_tasks, "_invoke", fake)
    with pytest.raises(llm_tasks.LLMError):
        await llm_tasks.classify_reply("t", None, "x")


def test_database_url_conversion():
    u = build_async_url("postgresql://user:p%40ss@host:5432/db?sslmode=require&channel_binding=require")
    assert u.drivername == "postgresql+asyncpg"
    assert dict(u.query) == {"ssl": "require"}
    assert u.password == "p@ss"
    assert build_async_url("postgres://u:p@h/db").drivername == "postgresql+asyncpg"
    assert build_async_url("sqlite:///x.db").drivername == "sqlite+aiosqlite"
