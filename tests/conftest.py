"""Test setup: throwaway SQLite database, fake mail, scripted fake AI.
Nothing here touches Gmail, Groq or your real database."""
import json
import os
import tempfile

_DB = os.path.join(tempfile.mkdtemp(), "test.db")
# Set TEST_DATABASE_URL to run the same tests against PostgreSQL instead of SQLite.
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite:///{_DB}"
os.environ["ENABLE_SCHEDULER"] = "false"
os.environ["GMAIL_SMTP_USER"] = "agent@dhruva.test"
os.environ["GMAIL_SMTP_PASSWORD"] = "x"
os.environ["TIMEZONE"] = "Asia/Kolkata"
os.environ["API_KEY"] = ""
os.environ["EMAIL_DRY_RUN"] = "false"
os.environ["ASYNC_DELIVERY"] = "false"   # existing tests expect the email to be sent inside create_task
# Pin the follow-up policy so tests never depend on a developer's personal .env
# (an environment variable set here wins over the same name in .env).
os.environ["FOLLOWUP_GAP_FAR_HOURS"] = "72"
os.environ["FOLLOWUP_GAP_MID_HOURS"] = "48"
os.environ["FOLLOWUP_GAP_NEAR_HOURS"] = "24"
os.environ["FOLLOWUP_GAP_OVERDUE_HOURS"] = "24"
os.environ["MAX_UNANSWERED_REMINDERS"] = "3"
os.environ["OVERDUE_ESCALATE_AFTER_HOURS"] = "24"
os.environ["WORK_START_HOUR"] = "9"
os.environ["WORK_END_HOUR"] = "18"
os.environ["SKIP_WEEKENDS"] = "true"

import pytest
import pytest_asyncio

from app import email_client, llm_tasks
from app.database import async_engine, async_session
from app.email_client import InboundEmail
from app.models import Base, Division, Employee, Team


@pytest_asyncio.fixture(autouse=True)
async def fresh_db():
    await async_engine.dispose()   # each test has its own event loop; drop pooled connections
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await async_engine.dispose()


@pytest_asyncio.fixture
async def org():
    """CEO -> Anita (QA lead) -> Ravi Kumar, Meena; Design: Suresh (lead), Ravi Sharma."""
    async with async_session() as s:
        q, e = Division(name="Quality"), Division(name="Engineering")
        s.add_all([q, e])
        await s.flush()
        qa, design = Team(name="QA", division_id=q.id), Team(name="Design", division_id=e.id)
        s.add_all([qa, design])
        await s.flush()
        ceo = Employee(full_name="Test CEO", email="ceo@example.com", is_leader=True)
        s.add(ceo)
        await s.flush()

        def emp(name, email, team, mgr, leader=False):
            return Employee(full_name=name, email=email, team_id=team.id,
                            manager_id=mgr.id if mgr else None, is_leader=leader)
        anita = emp("Anita Rao", "anita@example.com", qa, ceo, True)
        suresh = emp("Suresh Nair", "suresh@example.com", design, ceo, True)
        s.add_all([anita, suresh])
        await s.flush()
        s.add_all([
            emp("Ravi Kumar", "ravi.kumar@example.com", qa, anita),
            emp("Meena Iyer", "meena@example.com", qa, anita),
            emp("Ravi Sharma", "ravi.sharma@example.com", design, suresh),
        ])
        await s.commit()
    return True


@pytest.fixture
def sent(monkeypatch):
    """Capture every email the system tries to send."""
    box = []
    counter = iter(range(10**6))

    async def fake_send(to, subject, body, cc=None, in_reply_to=None, task_id=None, html=None, attachments=None):
        mid = f"<sent{next(counter)}@test>"
        box.append({"to": to, "subject": subject, "body": body, "html": html, "mid": mid, "in_reply_to": in_reply_to,
                    "cc": cc, "attachments": [(f.filename, f.content_type, f.data) for f in attachments or []]})
        return mid

    monkeypatch.setattr(email_client, "send_email", fake_send)
    return box


class FakeLLM:
    """Stands in for the AI. Status = simple keyword rules; assignment/pick are scripted."""
    def __init__(self):
        self.assign_json = {"is_assignment": False, "items": []}
        self.notes_json = {"items": []}
        self.pick_id = None
        self.confidence = "high"          # tests set "low" to make the AI unsure
        self.fail = False
        self.calls = 0

    async def __call__(self, system: str, user: str) -> str:
        self.calls += 1
        if self.fail:
            raise RuntimeError("model offline")
        if "report its status" in system:
            body = user.split("Email reply:")[-1].lower()
            if any(w in body for w in ("finished", "done", "completed")):
                st = "done"
            elif any(w in body for w in ("blocked", "stuck", "waiting for")):
                st = "blocked"
            elif any(w in body for w in ("delay", "more time", "late")):
                st = "delayed"
            elif body.strip() in ("thanks", "thank you", "ok thanks"):
                st = "none"
            else:
                st = "on-track"
            return json.dumps({"status": st, "summary": body.strip()[:60],
                               "blocker": "vendor part" if st == "blocked" else None,
                               "confidence": self.confidence})
        if "ACTION ITEMS" in system:
            return json.dumps(self.notes_json)
        if "extract work assignments" in system:
            return json.dumps(self.assign_json)
        if "several open tasks" in system:
            return json.dumps({"task_id": self.pick_id})
        return "{}"


@pytest.fixture
def llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(llm_tasks, "_invoke", fake)
    return fake


@pytest.fixture
def mailbox(monkeypatch):
    """Fake mailbox. Put InboundEmail objects in `.emails`; read marks in `.read`."""
    class Box:
        emails: list = []
        read: list = []
    box = Box()
    box.emails, box.read = [], []

    async def fake_fetch(skip_uids=None, limit=None):
        return [m for m in box.emails if m.uid not in (skip_uids or set())][: (limit or 50)]

    async def fake_mark(uids):
        box.read.extend(uids)

    monkeypatch.setattr(email_client, "fetch_unread", fake_fetch)
    monkeypatch.setattr(email_client, "mark_as_read", fake_mark)
    return box


_uid = iter(range(1000, 10**6))


def make_email(frm, subject="", body="", **kw) -> InboundEmail:
    uid = kw.pop("uid", next(_uid))
    return InboundEmail(
        uid=uid, message_id=kw.pop("message_id", f"<in{uid}@test>"),
        from_email=frm, from_name="", subject=subject, body=body, **kw,
    )