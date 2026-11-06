import httpx
import pytest_asyncio
from httpx import ASGITransport

from app.main import app


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def test_health_and_dashboard_page(client):
    assert (await client.get("/health")).json() == {"status": "ok"}
    r = await client.get("/dashboard")
    assert r.status_code == 200 and "Dhruva" in r.text


async def test_org_setup_and_task_flow(client, sent):
    d = (await client.post("/org/divisions", json={"name": "Quality"})).json()
    t = (await client.post("/org/teams", json={"name": "QA", "division_id": d["id"]})).json()
    boss = (await client.post("/org/employees", json={"full_name": "Boss", "email": "BOSS@x.com", "is_leader": True})).json()
    assert boss["email"] == "boss@x.com"                       # normalised to lower case
    assert (await client.post("/org/employees", json={"full_name": "Dup", "email": "boss@x.com"})).status_code == 409
    ravi = (await client.post("/org/employees", json={"full_name": "Ravi", "email": "ravi@x.com", "team_id": t["id"]})).json()

    r = await client.post("/tasks", json={"title": "Review", "assignee": "ravi@x.com",
                                          "assigned_by_email": "boss@x.com", "due_date": "2030-05-01"})
    assert r.status_code == 201
    task = r.json()[0]
    assert task["status"] == "pending" and task["assignee_name"] == "Ravi" and task["team"] == "QA"
    assert sent[0]["to"] == "ravi@x.com"

    assert (await client.post("/tasks", json={"title": "x", "assignee": "nobody"})).status_code == 422
    assert (await client.post("/tasks", json={"title": "x", "assignee": "ravi@x.com", "due_date": "soon"})).status_code == 422

    assert (await client.patch(f"/tasks/{task['id']}", json={"status": "banana"})).status_code == 422
    ok = await client.patch(f"/tasks/{task['id']}", json={"status": "on-track"})
    assert ok.status_code == 200 and ok.json()["status"] == "on-track"

    assert len((await client.get("/tasks", params={"status": "on-track"})).json()) == 1
    assert len((await client.get("/tasks", params={"status": "done"})).json()) == 0
    kinds = [e["event_type"] for e in (await client.get(f"/tasks/{task['id']}/events")).json()]
    assert kinds == ["created", "delivered", "status_change"]

    co = (await client.get("/reports/company")).json()
    assert co["summary"]["total"] == 1 and co["headcount"] == 2
    assert (await client.get(f"/reports/team/{t['id']}")).json()["members"][0]["name"] == "Ravi"
    assert (await client.get(f"/reports/person/{ravi['id']}")).json()["summary"]["total"] == 1
    assert (await client.get(f"/reports/division/{d['id']}")).status_code == 200
    assert (await client.get("/reports/team/999")).status_code == 404


async def test_api_key_is_enforced_when_set(client, monkeypatch):
    monkeypatch.setenv("API_KEY", "secret123")
    assert (await client.get("/tasks")).status_code == 401
    assert (await client.get("/tasks", headers={"X-API-Key": "wrong"})).status_code == 401
    assert (await client.get("/tasks", headers={"X-API-Key": "secret123"})).status_code == 200
    assert (await client.get("/health")).status_code == 200      # health stays public


async def test_chat_endpoint_passes_history_and_validates(client, monkeypatch):
    seen = {}

    async def fake_chat(message, as_email, history=None, attachment_ids=None):
        seen.update(message=message, as_email=as_email, history=history)
        return "hello back"
    monkeypatch.setattr("app.routes_agent.chat", fake_chat)

    body = {"message": "Ravi Kumar", "as_email": "ceo@example.com",
            "history": [{"role": "user", "content": "Ask Ravi"}, {"role": "assistant", "content": "Which Ravi?"}]}
    r = await client.post("/agent", json=body)
    assert r.status_code == 200 and r.json() == {"reply": "hello back", "tasks": []}
    assert seen["history"] == body["history"] and seen["as_email"] == "ceo@example.com"

    assert (await client.post("/agent", json={"message": "hi"})).status_code == 200            # history optional
    assert (await client.post("/agent", json={"message": ""})).status_code == 422             # empty message
    bad = {"message": "hi", "history": [{"role": "system", "content": "x"}]}
    assert (await client.post("/agent", json=bad)).status_code == 422                          # only user/assistant


async def test_chat_model_failure_gives_readable_error(client, monkeypatch):
    async def boom(*a, **k):
        raise ValueError("bad api key")
    monkeypatch.setattr("app.routes_agent.chat", boom)
    r = await client.post("/agent", json={"message": "hi"})
    assert r.status_code == 502 and "ValueError: bad api key" in r.json()["detail"]
