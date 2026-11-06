from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_session
from .security import require_api_key
from .services import impact, reports

router = APIRouter(prefix="/reports", tags=["reports"], dependencies=[Depends(require_api_key)])


@router.get("/company")
async def company(session: AsyncSession = Depends(get_session)):
    return await reports.company_report(session)


@router.get("/impact")
async def impact_numbers(days: int = Query(7, ge=1, le=3650, description="look back this many days"),
                         session: AsyncSession = Depends(get_session)):
    """What the agent did, how fast, and an estimate of the time it saved (see services/impact.py)."""
    return await impact.compute(session, days=days)


@router.get("/division/{division_id}")
async def division(division_id: int, session: AsyncSession = Depends(get_session)):
    r = await reports.division_report(session, division_id)
    if not r:
        raise HTTPException(404, "Division not found")
    return r


@router.get("/team/{team_id}")
async def team(team_id: int, session: AsyncSession = Depends(get_session)):
    r = await reports.team_report(session, team_id)
    if not r:
        raise HTTPException(404, "Team not found")
    return r


@router.get("/person/{emp_id}")
async def person(emp_id: int, session: AsyncSession = Depends(get_session)):
    r = await reports.person_report(session, emp_id)
    if not r:
        raise HTTPException(404, "Employee not found")
    return r
