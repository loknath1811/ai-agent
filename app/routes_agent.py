from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from . import directory, metrics
from .agent import chat, collect_created_tasks
from .database import async_session
from .llm_tasks import AssignmentItem, LLMError
from .schemas import (AttachmentRead, ChatIn, ChatOut, ChatTask, NotesConfirmIn, NotesConfirmOut, NotesConfirmResult,
                      NotesIn, NotesPreviewOut)
from .security import require_api_key
from .config import get_settings
from .services import attachments as attachment_service
from .services import brief as brief_service, followup, inbox, notes as notes_service
from .services.assign import assign_from_items

router = APIRouter(prefix="/agent", tags=["agent"], dependencies=[Depends(require_api_key)])


@router.post("", response_model=ChatOut)
async def talk_to_agent(body: ChatIn):
    """Plain-language chat: assign work, ask for status."""
    try:
        created = collect_created_tasks()
        reply = await chat(body.message, body.as_email, [t.model_dump() for t in body.history],
                           attachment_ids=body.attachment_ids)
        return ChatOut(reply=reply, tasks=created)
    except Exception as e:
        raise HTTPException(502, f"The AI model failed: {type(e).__name__}: {e}")


@router.post("/attachments", response_model=AttachmentRead, status_code=201)
async def upload_attachment(as_email: str = Form(...), file: UploadFile = File(...)):
    """Upload one file from the chat box. Nothing is emailed yet: the file is sent when a task is
    assigned in a chat message that lists the returned id in `attachment_ids`."""
    limit = get_settings().max_attachment_mb * 1024 * 1024
    data = await file.read(limit + 1)                  # never read more than one byte past the limit
    async with async_session() as session:
        actor = await _leader_or_403(session, as_email)
        try:
            return await attachment_service.save_upload(session, actor, file.filename, data)
        except attachment_service.AttachmentError as e:
            raise HTTPException(413 if len(data) > limit else 422, str(e))


@router.post("/process-inbox")
async def run_inbox_now():
    """Read the agent mailbox right now (the scheduler also does this automatically)."""
    try:
        return await inbox.process_inbox()
    except Exception as e:
        raise HTTPException(502, f"Mailbox check failed: {type(e).__name__}: {e}")


@router.post("/run-followups")
async def run_followups_now(force: bool = True):
    """Run the follow-up/escalation pass now. force=true ignores working hours."""
    async with async_session() as session:
        return await followup.run_followups(session, force=force)


async def _leader_or_403(session, as_email):
    actor = await directory.get_by_email(session, as_email) if as_email else None
    if actor is None or not actor.active or not actor.is_leader:
        raise HTTPException(403, "Only leaders can assign work. Pick a leader in the 'chatting as' box.")
    return actor


@router.post("/notes/preview", response_model=NotesPreviewOut)
async def preview_meeting_notes(body: NotesIn):
    """Read meeting notes and PROPOSE tasks. Nothing is created and no email is sent."""
    async with async_session() as session:
        actor = await _leader_or_403(session, body.as_email)
        try:
            async with metrics.measure("notes_preview"):
                return await notes_service.preview_notes(session, actor, body.notes)
        except LLMError as e:
            raise HTTPException(502, f"The AI could not read the notes: {e}")
        except Exception as e:
            raise HTTPException(502, f"The AI model failed: {type(e).__name__}: {e}")


@router.post("/notes/confirm", response_model=NotesConfirmOut)
async def confirm_meeting_notes(body: NotesConfirmIn):
    """Create the tasks the leader approved (possibly edited). Each row succeeds or fails on its own."""
    results = []
    async with async_session() as session:
        actor = await _leader_or_403(session, body.as_email)
        for it in body.items:
            res = await assign_from_items(session, actor, [AssignmentItem(
                title=it.title.strip(), assignee=it.assignee.strip(), description=it.description,
                due_date=it.due_date, fan_out=it.fan_out)])
            results.append(NotesConfirmResult(
                index=it.index,
                created=[ChatTask(id=t.id, title=t.title, assignee=t.assignee.full_name) for t in res.created],
                problems=res.problems, notes=res.notes))
    return NotesConfirmOut(results=results)


@router.get("/brief/preview")
async def preview_brief():
    """What the morning brief would say right now, and who would get it. Sends nothing."""
    async with async_session() as session:
        return await brief_service.preview(session)


@router.post("/brief/send")
async def send_brief_now():
    """Send the morning brief right now (the scheduler also sends it every working morning)."""
    async with async_session() as session:
        return await brief_service.send_brief(session)
