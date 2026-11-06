"""Meeting notes: preview proposes tasks (creating nothing), confirm creates what the leader approved."""
import httpx
import pytest_asyncio
from httpx import ASGITransport
from sqlalchemy import func, select

from app.database import async_session
from app.main import app
from app.models import Task

NOTES = ("Product review, 29 Oct. Ravi Kumar will finish the NCR root-cause review by 2030-01-10. "
         "Meena Iyer to send the updated drawings to the vendor by 2030-01-13. "
         "The QA team needs to re-test batch 14. Somebody should look at the budget. "
         "We also discussed the schedule, no action needed.")


def item(title, assignee, evidence, due="2030-01-10", fan_out=False):
    return {"title": title, "assignee": assignee, "description": f"Please {title.lower()}.",
            "due_date": due, "fan_out": fan_out, "evidence": evidence}


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _task_count():
    async with async_session() as s:
        return (await s.execute(select(func.count()).select_from(Task))).scalar_one()


async def test_preview_proposes_rows_and_creates_nothing(client, org, llm, sent):
    llm.notes_json = {"items": [
        item("Finish the NCR root-cause review", "Ravi Kumar", "Ravi Kumar will finish the NCR root-cause review"),
        item("Send updated drawings to vendor", "Meena Iyer", "Meena Iyer to send the updated drawings to the vendor", "2030-01-13"),
        item("Re-test batch 14", "QA team", "The QA team needs to re-test batch 14", None, fan_out=True),
        item("Review the budget", None, "Somebody should look at the budget", None),
    ]}
    r = await client.post("/agent/notes/preview", json={"notes": NOTES, "as_email": "ceo@example.com"})
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert [x["title"] for x in rows][:2] == ["Finish the NCR root-cause review", "Send updated drawings to vendor"]
    assert rows[0]["recipients"] == [{"name": "Ravi Kumar", "email": "ravi.kumar@example.com"}]
    assert rows[0]["verified"] and rows[0]["problem"] is None
    team_row = rows[2]                                           # fan-out: every QA member except the sender
    assert {p["name"] for p in team_row["recipients"]} == {"Anita Rao", "Ravi Kumar", "Meena Iyer"}
    assert rows[3]["problem"] and "No owner" in rows[3]["problem"] and rows[3]["recipients"] == []
    assert await _task_count() == 0 and sent == []               # a preview changes nothing
    assert llm.calls == 1                                        # one AI call, however many items


async def test_preview_flags_invented_items_and_unclear_people(client, org, llm):
    llm.notes_json = {"items": [
        item("Order new servers", "Meena Iyer", "Meena will order the new servers"),      # not in the notes
        item("Check the design", "Ravi", "Ravi Kumar will finish the NCR root-cause review"),  # two Ravis
        item("Old thing", "Meena Iyer", "Meena Iyer to send the updated drawings to the vendor", "2001-01-01"),
    ]}
    rows = (await client.post("/agent/notes/preview", json={"notes": NOTES, "as_email": "ceo@example.com"})).json()["rows"]
    assert rows[0]["verified"] is False                          # evidence is not in the notes
    assert "more than one person" in rows[1]["problem"]
    assert "already passed" in rows[2]["problem"]


async def test_preview_with_no_action_items_returns_no_rows(client, org, llm):
    llm.notes_json = {"items": []}
    r = await client.post("/agent/notes/preview", json={"notes": NOTES, "as_email": "ceo@example.com"})
    assert r.status_code == 200 and r.json()["rows"] == []


async def test_only_leaders_can_use_notes(client, org, llm):
    for who in ("ravi.kumar@example.com", "nobody@example.com", None):
        r = await client.post("/agent/notes/preview", json={"notes": NOTES, "as_email": who})
        assert r.status_code == 403
    r = await client.post("/agent/notes/confirm", json={"as_email": "ravi.kumar@example.com", "items": [
        {"index": 0, "title": "X", "assignee": "Meena Iyer"}]})
    assert r.status_code == 403
    assert llm.calls == 0 and await _task_count() == 0


async def test_ai_failure_is_reported_not_swallowed(client, org, llm):
    llm.fail = True
    r = await client.post("/agent/notes/preview", json={"notes": NOTES, "as_email": "ceo@example.com"})
    assert r.status_code == 502


async def test_notes_too_short_or_too_long_are_rejected(client, org, llm):
    for text in ("short", "x" * 12001):
        r = await client.post("/agent/notes/preview", json={"notes": text, "as_email": "ceo@example.com"})
        assert r.status_code == 422
    assert llm.calls == 0


async def test_confirm_creates_tasks_row_by_row(client, org, llm, sent):
    r = await client.post("/agent/notes/confirm", json={"as_email": "ceo@example.com", "items": [
        {"index": 0, "title": "Finish NCR review", "assignee": "Ravi Kumar", "due_date": "2030-01-10"},
        {"index": 1, "title": "Look at budget", "assignee": "Ravi", "due_date": "2030-01-10"},      # ambiguous
        {"index": 2, "title": "Re-test batch 14", "assignee": "QA team", "fan_out": True},
    ]})
    assert r.status_code == 200
    res = {x["index"]: x for x in r.json()["results"]}
    assert [t["assignee"] for t in res[0]["created"]] == ["Ravi Kumar"]
    assert res[1]["created"] == [] and "more than one person" in res[1]["problems"][0]
    assert len(res[2]["created"]) == 3
    assert await _task_count() == 4                              # the failed row did not stop the others
    assert llm.calls == 0                                        # confirming uses no AI at all


async def test_confirm_rejects_bad_dates_and_empty_lists(client, org):
    bad = {"index": 0, "title": "X", "assignee": "Meena Iyer", "due_date": "next friday"}
    assert (await client.post("/agent/notes/confirm", json={"as_email": "ceo@example.com", "items": [bad]})).status_code == 422
    assert (await client.post("/agent/notes/confirm", json={"as_email": "ceo@example.com", "items": []})).status_code == 422
