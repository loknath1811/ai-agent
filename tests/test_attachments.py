"""Files attached in the chat are emailed with the assignment."""
import dataclasses
from datetime import timedelta

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app import agent, directory, email_client
from app.config import get_settings
from app.database import async_session
from app.llm_tasks import AssignmentItem
from app.main import app
from app.models import Attachment, TaskAttachment, TaskEvent
from app.services import attachments as att
from app.services import delivery
from app.services import tasks as task_service
from app.services.assign import assign_from_items
from app.timeutil import utcnow

PDF = b"%PDF-1.4 fake pdf bytes \x00\x01\x02"


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _upload(email="ceo@example.com", name="spec.pdf", data=PDF):
    async with async_session() as s:
        who = await directory.get_by_email(s, email)
        return await att.save_upload(s, who, name, data)


async def _assign(att_ids, assignee="Meena", who="ceo@example.com"):
    async with async_session() as s:
        boss = await directory.get_by_email(s, who)
        return await assign_from_items(
            s, boss, [AssignmentItem(title="NCR review", assignee=assignee, due_date="2030-01-10")],
            attachment_ids=att_ids)


# ---------------------------------------------------------------- validation
def test_filename_is_cleaned():
    assert att.clean_filename("../../etc/passwd") == "passwd"
    assert att.clean_filename("C:\\Users\\me\\report.pdf") == "report.pdf"
    assert att.clean_filename("a\x00b\r\n.pdf") == "ab.pdf"
    assert len(att.clean_filename("x" * 300 + ".pdf")) <= 120 and att.clean_filename("x" * 300 + ".pdf").endswith(".pdf")


@pytest.mark.parametrize("name", ["setup.exe", "run.BAT", "x.js", "tool.ps1"])
def test_programs_are_refused(name):
    with pytest.raises(att.AttachmentError, match="blocked"):
        att.check_upload(name, b"data")


def test_empty_and_oversized_files_are_refused(monkeypatch):
    with pytest.raises(att.AttachmentError, match="empty"):
        att.check_upload("a.pdf", b"")
    small = dataclasses.replace(get_settings(), max_attachment_mb=1)
    monkeypatch.setattr(att, "get_settings", lambda: small)
    with pytest.raises(att.AttachmentError, match="limit is 1 MB"):
        att.check_upload("big.pdf", b"x" * (1024 * 1024 + 1))


# ---------------------------------------------------------------- the email itself
async def test_assignment_email_carries_the_files(org, sent):
    a = await _upload()
    b = await _upload(name="data.xlsx", data=b"PK\x03\x04sheet")
    await _assign([a.id, b.id])
    mail = sent[0]
    assert mail["to"] == "meena@example.com"
    assert [(n, d) for n, _, d in mail["attachments"]] == [("spec.pdf", PDF), ("data.xlsx", b"PK\x03\x04sheet")]
    assert mail["attachments"][0][1] == "application/pdf"
    assert "spec.pdf" in mail["body"] and "data.xlsx" in mail["body"]    # the body tells the reader what is attached


async def test_task_without_files_is_unchanged(org, sent):
    await _assign([])
    assert sent[0]["attachments"] == [] and "Attached" not in sent[0]["body"]


async def test_history_records_the_files(org, sent):
    a = await _upload()
    await _assign([a.id])
    async with async_session() as s:
        ev = (await s.execute(select(TaskEvent).where(TaskEvent.event_type == "delivered"))).scalar_one()
    assert "spec.pdf" in ev.detail


async def test_real_mime_message_has_the_attachment(monkeypatch):
    seen = {}

    async def fake_smtp(msg, **kw):
        seen["msg"] = msg
    monkeypatch.setattr(email_client.aiosmtplib, "send", fake_smtp)
    f = att.FileToSend("spec.pdf", "application/pdf", PDF)
    await email_client.send_email("a@b.com", "[Task #1] x", "plain body", html="<p>h</p>", attachments=[f])
    msg = seen["msg"]
    parts = list(msg.iter_attachments())
    assert len(parts) == 1 and parts[0].get_filename() == "spec.pdf"
    assert parts[0].get_content() == PDF and parts[0].get_content_type() == "application/pdf"
    assert "plain body" in msg.get_body(("plain",)).get_content()       # body still readable
    assert "<p>h</p>" in msg.get_body(("html",)).get_content()


# ---------------------------------------------------------------- background sender + retries
@pytest.fixture
def background(monkeypatch):
    s = dataclasses.replace(get_settings(), async_delivery=True)
    monkeypatch.setattr(task_service, "get_settings", lambda: s)


async def test_background_sender_attaches_files(org, sent, background):
    a = await _upload()
    await _assign([a.id])
    assert sent == []                                   # nothing sent yet, files already linked
    async with async_session() as s:
        assert await delivery.deliver_pending(s) == 1
    assert sent[0]["attachments"][0][0] == "spec.pdf"


async def test_retry_after_failure_still_has_the_files(org, sent, background, monkeypatch):
    a = await _upload()
    await _assign([a.id])
    good = email_client.send_email                      # the capturing fake from the `sent` fixture

    async def down(*args, **kw):
        raise RuntimeError("smtp down")
    monkeypatch.setattr(email_client, "send_email", down)
    async with async_session() as s:
        assert await delivery.deliver_pending(s) == 0   # first attempt fails, task stays "waiting"
    assert sent == []
    monkeypatch.setattr(email_client, "send_email", good)
    async with async_session() as s:
        assert await delivery.deliver_pending(s, now=utcnow() + timedelta(minutes=2)) == 1
    assert sent[-1]["attachments"][0][0] == "spec.pdf"  # the retry carried the file too


async def test_team_fan_out_gives_every_task_the_files(org, sent):
    a = await _upload()
    async with async_session() as s:
        boss = await directory.get_by_email(s, "ceo@example.com")
        res = await assign_from_items(s, boss, [AssignmentItem(title="Safety training", assignee="QA team",
                                                               due_date="2030-01-10", fan_out=True)],
                                      attachment_ids=[a.id])
    assert len(res.created) >= 2
    assert len(sent) == len(res.created) and all(m["attachments"][0][0] == "spec.pdf" for m in sent)
    async with async_session() as s:
        links = (await s.execute(select(TaskAttachment))).scalars().all()
    assert len(links) == len(res.created)


# ---------------------------------------------------------------- who may use which file
async def test_only_the_uploader_can_use_a_file(org, sent):
    a = await _upload(email="ceo@example.com")
    async with async_session() as s:
        anita = await directory.get_by_email(s, "anita@example.com")
        with pytest.raises(att.AttachmentError, match="not found"):
            await att.load_for_actor(s, anita, [a.id])
        with pytest.raises(att.AttachmentError, match="not found"):
            await att.load_for_actor(s, anita, [99999])


async def test_count_and_total_size_limits(org, monkeypatch):
    tight = dataclasses.replace(get_settings(), max_attachments=2, max_attachments_total_mb=1)
    monkeypatch.setattr(att, "get_settings", lambda: tight)
    ids = [(await _upload(name=f"f{i}.txt", data=b"x" * 600_000)).id for i in range(3)]
    async with async_session() as s:
        ceo = await directory.get_by_email(s, "ceo@example.com")
        with pytest.raises(att.AttachmentError, match="At most 2"):
            await att.load_for_actor(s, ceo, ids)
        with pytest.raises(att.AttachmentError, match="total"):
            await att.load_for_actor(s, ceo, ids[:2])


async def test_unused_uploads_are_purged_but_used_ones_stay(org, sent):
    used, unused = await _upload(name="used.pdf"), await _upload(name="unused.pdf")
    await _assign([used.id])
    async with async_session() as s:
        for a in (await s.execute(select(Attachment))).scalars():
            a.created_at = utcnow() - timedelta(hours=48)
        await s.commit()
        assert await att.purge_orphans(s) == 1
        await s.commit()
        names = [a.filename for a in (await s.execute(select(Attachment))).scalars()]
    assert names == ["used.pdf"]


# ---------------------------------------------------------------- HTTP upload endpoint
async def test_upload_endpoint(org, client):
    r = await client.post("/agent/attachments", data={"as_email": "ceo@example.com"},
                          files={"file": ("brief.pdf", PDF, "application/pdf")})
    assert r.status_code == 201
    body = r.json()
    assert body["filename"] == "brief.pdf" and body["size"] == len(PDF) and body["content_type"] == "application/pdf"
    assert "data" not in body                                   # bytes are never sent back


async def test_upload_endpoint_rules(org, client, monkeypatch):
    f = {"file": ("a.pdf", PDF, "application/pdf")}
    # not a leader
    assert (await client.post("/agent/attachments", data={"as_email": "meena@example.com"}, files=f)).status_code == 403
    # nobody selected
    assert (await client.post("/agent/attachments", data={"as_email": "nobody@example.com"}, files=f)).status_code == 403
    # a program
    r = await client.post("/agent/attachments", data={"as_email": "ceo@example.com"},
                          files={"file": ("virus.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 422 and "blocked" in r.json()["detail"]
    # empty
    r = await client.post("/agent/attachments", data={"as_email": "ceo@example.com"}, files={"file": ("e.txt", b"", "text/plain")})
    assert r.status_code == 422
    # too big
    small = dataclasses.replace(get_settings(), max_attachment_mb=1)
    from app import routes_agent
    monkeypatch.setattr(att, "get_settings", lambda: small)
    monkeypatch.setattr(routes_agent, "get_settings", lambda: small)
    r = await client.post("/agent/attachments", data={"as_email": "ceo@example.com"},
                          files={"file": ("big.bin", b"x" * (1024 * 1024 + 5), "application/octet-stream")})
    assert r.status_code == 413


async def test_upload_needs_the_api_key_when_set(org, client, monkeypatch):
    monkeypatch.setenv("API_KEY", "secret123")
    f = {"file": ("a.pdf", PDF, "application/pdf")}
    assert (await client.post("/agent/attachments", data={"as_email": "ceo@example.com"}, files=f)).status_code == 401


# ---------------------------------------------------------------- the chat, end to end
class ToolFake(FakeMessagesListChatModel):
    seen: list = []

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, *args, **kwargs):
        type(self).seen.append([m.content for m in messages])
        return super()._generate(messages, *args, **kwargs)


def _script(monkeypatch):
    ToolFake.seen = []
    monkeypatch.setattr(agent, "get_chat_model", lambda: ToolFake(responses=[
        AIMessage(content="", tool_calls=[{"name": "assign_work", "id": "1",
                                           "args": {"instruction": "Ask Meena to do the NCR review"}}]),
        AIMessage(content="Assigned.")]))


async def test_chat_sends_attached_files_with_the_task(org, sent, llm, monkeypatch):
    llm.assign_json = {"is_assignment": True, "items": [{"title": "NCR review", "assignee": "Meena", "due_date": "2030-01-10"}]}
    _script(monkeypatch)
    a = await _upload()
    await agent.chat("Ask Meena to do the NCR review", "ceo@example.com", attachment_ids=[a.id])
    assert sent[0]["to"] == "meena@example.com" and sent[0]["attachments"][0][0] == "spec.pdf"
    assert any("spec.pdf" in str(c) for c in ToolFake.seen[0])         # the model was told a file came along


async def test_chat_tool_refuses_someone_elses_file(org, sent, llm, monkeypatch):
    llm.assign_json = {"is_assignment": True, "items": [{"title": "X", "assignee": "Meena", "due_date": "2030-01-10"}]}
    _script(monkeypatch)
    a = await _upload(email="ceo@example.com")
    await agent.chat("Ask Meena to do X", "anita@example.com", attachment_ids=[a.id])   # Anita did not upload it
    assert sent == []                                                  # no task, no email: nothing leaked
    async with async_session() as s:
        from app.models import Task
        assert (await s.execute(select(Task))).scalars().first() is None


async def test_chat_endpoint_accepts_attachment_ids(org, sent, llm, client, monkeypatch):
    llm.assign_json = {"is_assignment": True, "items": [{"title": "NCR review", "assignee": "Meena", "due_date": "2030-01-10"}]}
    _script(monkeypatch)
    up = await client.post("/agent/attachments", data={"as_email": "ceo@example.com"},
                           files={"file": ("plan.docx", b"docx-bytes", "application/octet-stream")})
    r = await client.post("/agent", json={"message": "Ask Meena to do the NCR review", "as_email": "ceo@example.com",
                                          "attachment_ids": [up.json()["id"]]})
    assert r.status_code == 200 and r.json()["tasks"]
    assert sent[0]["attachments"] == [("plan.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", b"docx-bytes")]
