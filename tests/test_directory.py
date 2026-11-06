from app import directory
from app.database import async_session


async def test_resolution_rules(org):
    async with async_session() as s:
        r = await directory.resolve(s, "ravi.kumar@example.com")
        assert r.kind == "person" and r.employee.full_name == "Ravi Kumar"

        r = await directory.resolve(s, "Ravi Kumar")
        assert r.kind == "person" and r.employee.email == "ravi.kumar@example.com"

        r = await directory.resolve(s, "Ravi")                 # two Ravis -> must ask
        assert r.kind == "ambiguous" and len(r.candidates) == 2
        assert "Which one" in r.message

        r = await directory.resolve(s, "Ravi from QA")         # team hint disambiguates
        assert r.kind == "person" and r.employee.email == "ravi.kumar@example.com"

        r = await directory.resolve(s, "the QA team")
        assert r.kind == "team" and r.team.name == "QA"

        r = await directory.resolve(s, "Meena")
        assert r.kind == "person"

        r = await directory.resolve(s, "Zebulon")              # unknown -> none, no invention
        assert r.kind == "none"

        r = await directory.resolve(s, "nobody@example.com")
        assert r.kind == "none"


async def test_escalation_chain_and_team_leader(org):
    async with async_session() as s:
        ravi = await directory.get_by_email(s, "ravi.kumar@example.com")
        chain = await directory.escalation_chain(s, ravi)
        assert [p.email for p in chain] == ["anita@example.com", "ceo@example.com"]
        lead = await directory.team_leader(s, ravi.team_id)
        assert lead.email == "anita@example.com"
