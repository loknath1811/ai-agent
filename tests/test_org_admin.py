"""People admin: add/edit/deactivate with checks, bulk import preview/apply, change log."""
from datetime import timedelta

import httpx
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import select

from app import directory
from app.database import async_session
from app.llm_tasks import AssignmentItem
from app.main import app
from app.models import Employee, Task
from app.services import followup
from app.services.assign import assign_from_items
from app.timeutil import utcnow


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _emp(client, email):
    return next(e for e in (await client.get("/org/employees")).json() if e["email"] == email)


async def test_add_person_validations(client):
    t = (await client.post("/org/teams", json={"name": "QA"})).json()
    ok = await client.post("/org/employees", json={"full_name": " Asha ", "email": " ASHA@x.com ",
                                                    "team_id": t["id"], "title": " Engineer "})
    assert ok.status_code == 201
    assert ok.json()["email"] == "asha@x.com" and ok.json()["full_name"] == "Asha" and ok.json()["title"] == "Engineer"
    bad = [
        ({"full_name": "A", "email": "not-an-email"}, 422),                    # invalid address
        ({"full_name": "  ", "email": "b@x.com"}, 422),                         # empty name
        ({"full_name": "Dup", "email": "asha@x.com"}, 409),                     # duplicate
        ({"full_name": "A", "email": "c@x.com", "team_id": 999}, 422),          # team missing
        ({"full_name": "A", "email": "d@x.com", "manager_id": 999}, 422),       # manager missing
        ({"full_name": "A", "email": "agent@dhruva.test"}, 422),                # the agent's own mailbox
    ]
    for body, code in bad:
        assert (await client.post("/org/employees", json=body)).status_code == code, body


async def test_manager_rules_and_loop_protection(client):
    a = (await client.post("/org/employees", json={"full_name": "A", "email": "a@x.com"})).json()
    b = (await client.post("/org/employees", json={"full_name": "B", "email": "b@x.com", "manager_id": a["id"]})).json()
    assert (await client.patch(f"/org/employees/{a['id']}", json={"manager_id": a["id"]})).status_code == 422
    assert (await client.patch(f"/org/employees/{a['id']}", json={"manager_id": b["id"]})).status_code == 422  # loop
    await client.patch(f"/org/employees/{b['id']}", json={"active": False})
    c = await client.post("/org/employees", json={"full_name": "C", "email": "c@x.com", "manager_id": b["id"]})
    assert c.status_code == 422 and "inactive" in c.json()["detail"]


async def test_edit_changes_email_and_blocks_duplicates(client):
    a = (await client.post("/org/employees", json={"full_name": "A", "email": "a@x.com"})).json()
    await client.post("/org/employees", json={"full_name": "B", "email": "b@x.com"})
    assert (await client.patch(f"/org/employees/{a['id']}", json={"email": "b@x.com"})).status_code == 409
    ok = await client.patch(f"/org/employees/{a['id']}", json={"email": "A2@x.com", "title": "Lead"})
    assert ok.json()["email"] == "a2@x.com" and ok.json()["title"] == "Lead"


async def test_division_and_team_names_are_unique_case_insensitively(client):
    assert (await client.post("/org/divisions", json={"name": "Quality"})).status_code == 201
    assert (await client.post("/org/divisions", json={"name": "quality"})).status_code == 409
    assert (await client.post("/org/teams", json={"name": "QA"})).status_code == 201
    assert (await client.post("/org/teams", json={"name": "qa"})).status_code == 409
    t = (await client.get("/org/teams")).json()[0]
    assert (await client.patch(f"/org/teams/{t['id']}", json={"name": "QA Team"})).json()["name"] == "QA Team"


async def test_deactivated_person_is_not_chased_or_escalated(org, sent, client):
    async with async_session() as s:
        res = await assign_from_items(s, await directory.get_by_email(s, "ceo@example.com"),
                                      [AssignmentItem(title="Audit", assignee="Meena", due_date="2030-01-01")])
        tid = res.created[0].id
        t = await s.get(Task, tid)
        t.next_followup_at = utcnow() - timedelta(hours=1)
        await s.commit()
    meena = await _emp(client, "meena@example.com")
    impact = (await client.get(f"/org/employees/{meena['id']}/impact")).json()
    assert impact["open_tasks"] == 1
    sent.clear()
    await client.patch(f"/org/employees/{meena['id']}", json={"active": False})
    async with async_session() as s:
        stats = await followup.run_followups(s, force=True)
    assert stats["reminders"] == 0 and sent == []
    await client.patch(f"/org/employees/{meena['id']}", json={"active": True})   # back: chased again
    async with async_session() as s:
        stats = await followup.run_followups(s, force=True)
    assert stats["reminders"] == 1


GOOD = """division,team,full_name,email,title,manager_email,is_leader
Quality,QA,Lead One,lead@x.com,QA Lead,,true
Quality,QA,Emp One,emp1@x.com,Engineer,lead@x.com,false
Quality,QA,Emp Two,emp2@x.com,Engineer,lead@x.com,false
"""


async def test_import_preview_does_not_save_and_apply_does(client):
    pv = (await client.post("/org/import/preview", json={"csv": GOOD})).json()
    assert pv["summary"] == {"add": 3, "update": 0, "unchanged": 0, "error": 0} and pv["applied"] is False
    assert (await client.get("/org/employees")).json() == []                       # nothing saved yet
    ap = (await client.post("/org/import/apply", json={"csv": GOOD})).json()
    assert ap["applied"] is True
    emp1 = await _emp(client, "emp1@x.com")
    lead = await _emp(client, "lead@x.com")
    assert emp1["manager_id"] == lead["id"] and lead["is_leader"] is True
    again = (await client.post("/org/import/preview", json={"csv": GOOD})).json()   # idempotent
    assert again["summary"]["unchanged"] == 3
    changes = (await client.get("/org/changes")).json()
    assert changes[0]["action"] == "import"


async def test_import_reports_row_errors_and_saves_nothing(client):
    bad = """division,team,full_name,email,title,manager_email,is_leader
Quality,QA,Good,good@x.com,,,false
Quality,QA,Bad Mail,nope,,,false
Quality,QA,Twin,good@x.com,,,false
Quality,QA,Orphan,orphan@x.com,,ghost@x.com,false
Quality,QA,Loop A,la@x.com,lb@x.com,false
Quality,QA,Loop B,lb@x.com,la@x.com,false
Quality,QA,Flag,flag@x.com,,,maybe
"""
    bad = bad.replace("Loop A,la@x.com,lb@x.com,false", "Loop A,la@x.com,,lb@x.com,false") \
             .replace("Loop B,lb@x.com,la@x.com,false", "Loop B,lb@x.com,,la@x.com,false")
    pv = (await client.post("/org/import/preview", json={"csv": bad})).json()
    by_line = {r["line"]: r for r in pv["rows"]}
    assert by_line[2]["action"] == "add"
    assert "valid email" in by_line[3]["problems"][0]
    assert "twice" in by_line[4]["problems"][0]
    assert "not in the file" in by_line[5]["problems"][0]
    assert "loop" in by_line[6]["problems"][0] and "loop" in by_line[7]["problems"][0]
    assert "true or false" in by_line[8]["problems"][0]
    r = await client.post("/org/import/apply", json={"csv": bad})
    assert r.status_code == 422
    assert (await client.get("/org/employees")).json() == []                        # all or nothing


async def test_import_bad_files_and_updates(client):
    assert (await client.post("/org/import/preview", json={"csv": ""})).status_code == 422
    assert (await client.post("/org/import/preview", json={"csv": "name,mail\nA,a@x.com\n"})).status_code == 422
    await client.post("/org/import/apply", json={"csv": GOOD})
    changed = GOOD.replace("Engineer,lead@x.com,false\nQuality,QA,Emp Two", "Senior Engineer,lead@x.com,false\nQuality,QA,Emp Two", 1)
    pv = (await client.post("/org/import/preview", json={"csv": changed})).json()
    assert pv["summary"]["update"] == 1 and pv["summary"]["unchanged"] == 2
    # a person missing from the file is NOT removed
    only_lead = "division,team,full_name,email,title,manager_email,is_leader\nQuality,QA,Lead One,lead@x.com,QA Lead,,true\n"
    await client.post("/org/import/apply", json={"csv": only_lead})
    assert all(e["active"] for e in (await client.get("/org/employees")).json())


async def test_deactivation_is_logged(client):
    a = (await client.post("/org/employees", json={"full_name": "A", "email": "a@x.com"})).json()
    await client.patch(f"/org/employees/{a['id']}", json={"active": False})
    actions = [c["action"] for c in (await client.get("/org/changes")).json()]
    assert actions[:2] == ["employee_deactivated", "employee_added"]


async def test_overview_returns_everything_in_one_call(client):
    d = (await client.post("/org/divisions", json={"name": "Quality"})).json()
    t = (await client.post("/org/teams", json={"name": "QA", "division_id": d["id"]})).json()
    await client.post("/org/employees", json={"full_name": "Asha", "email": "asha@x.com", "team_id": t["id"]})
    o = (await client.get("/org/overview")).json()
    assert [e["email"] for e in o["employees"]] == ["asha@x.com"]
    assert [x["name"] for x in o["teams"]] == ["QA"] and [x["name"] for x in o["divisions"]] == ["Quality"]
    assert o["changes"][0]["action"] == "employee_added"
