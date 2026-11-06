from datetime import datetime
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from .models import TaskStatus


# ---------- organization ----------
class DivisionIn(BaseModel):
    name: str


class DivisionRead(DivisionIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


class TeamIn(BaseModel):
    name: str
    division_id: Optional[int] = None


class TeamRead(TeamIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


class EmployeeIn(BaseModel):
    full_name: str
    email: str
    title: Optional[str] = None
    team_id: Optional[int] = None
    manager_id: Optional[int] = None
    is_leader: bool = False


class EmployeeUpdate(BaseModel):
    full_name: Optional[str] = None
    email: Optional[str] = None
    title: Optional[str] = None
    team_id: Optional[int] = None
    manager_id: Optional[int] = None
    is_leader: Optional[bool] = None
    active: Optional[bool] = None

class NameUpdate(BaseModel):
    name: Optional[str] = None


class TeamUpdate(BaseModel):
    name: Optional[str] = None
    division_id: Optional[int] = None


class ImportIn(BaseModel):
    csv: str
    
class EmployeeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    full_name: str
    email: str
    title: Optional[str] = None
    team_id: Optional[int] = None
    manager_id: Optional[int] = None
    is_leader: bool
    active: bool


# ---------- tasks ----------
class TaskCreate(BaseModel):
    title: str = Field(..., examples=["Complete the NCR root-cause review"])
    assignee: str = Field(..., description="Email, name or team name", examples=["ravi.kumar@example.com"])
    assigned_by_email: Optional[str] = None
    description: Optional[str] = None
    due_date: Optional[str] = Field(None, description="YYYY-MM-DD or YYYY-MM-DDTHH:MM (company local time)")


class TaskUpdate(BaseModel):
    """Manual override (e.g. by a manager). Normal updates arrive by email."""
    model_config = ConfigDict(use_enum_values=True)
    status: Optional[TaskStatus] = None
    due_date: Optional[str] = None


class CorrectIn(BaseModel):
    """A leader fixes (or confirms) the status the agent recorded."""
    model_config = ConfigDict(use_enum_values=True)
    status: TaskStatus
    as_email: Optional[str] = None


class TaskRead(BaseModel):
    id: int
    title: str
    description: Optional[str] = None
    status: str
    assignee_id: int
    assignee_name: str
    assignee_email: str
    team: Optional[str] = None
    assigned_by: Optional[str] = None
    due_date: Optional[datetime] = None
    overdue: bool
    escalated: bool
    followup_count: int
    last_reply_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    delivery_status: Optional[str] = None     # waiting / sent / failed (the assignment email)
    delivery_error: Optional[str] = None
    status_check: Optional[str] = None        # "unconfirmed" while the employee is asked to confirm the status


class DeliveryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    delivery_status: str
    delivery_attempts: int
    delivery_error: Optional[str] = None


class EventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    event_type: str
    old_status: Optional[str] = None
    new_status: Optional[str] = None
    actor_id: Optional[int] = None
    detail: Optional[str] = None
    created_at: datetime


# ---------- agent chat ----------
class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=4000)


class ChatIn(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000,
                         examples=["Ask the QA team to complete the NCR review by Friday"])
    as_email: Optional[str] = Field(None, description="Who is speaking (must be a leader to assign work)")
    history: List[ChatTurn] = Field(
        default_factory=list,
        description="Earlier turns of this conversation, oldest first (the chat box sends these)",
    )
    attachment_ids: List[int] = Field(
        default_factory=list, max_length=20,
        description="Files uploaded with POST /agent/attachments; they are emailed with any task this message creates",
    )


class AttachmentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    filename: str
    content_type: str
    size: int


class NotesIn(BaseModel):
    notes: str = Field(..., min_length=20, max_length=12000)
    as_email: Optional[str] = Field(None, description="Who is importing the notes (must be a leader)")


class NotesRecipient(BaseModel):
    name: str
    email: str


class NotesRow(BaseModel):
    index: int
    title: str
    assignee: str
    due_date: str
    description: Optional[str] = None
    fan_out: bool = False
    evidence: str = ""
    verified: bool = True
    recipients: List[NotesRecipient] = []
    problem: Optional[str] = None
    note: Optional[str] = None


class NotesPreviewOut(BaseModel):
    rows: List[NotesRow]
    truncated: bool = False


class NotesConfirmItem(BaseModel):
    index: int
    title: str = Field(..., min_length=1, max_length=200)
    assignee: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(None, max_length=2000)
    due_date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    fan_out: bool = False


class NotesConfirmIn(BaseModel):
    as_email: Optional[str] = None
    items: List[NotesConfirmItem] = Field(..., min_length=1, max_length=25)


class NotesConfirmResult(BaseModel):
    index: int
    created: List["ChatTask"] = []
    problems: List[str] = []
    notes: List[str] = []


class NotesConfirmOut(BaseModel):
    results: List[NotesConfirmResult]


class ChatTask(BaseModel):
    """A task created during this chat turn, so the page can follow its email delivery."""
    id: int
    title: str
    assignee: str


class ChatOut(BaseModel):
    reply: str
    tasks: List[ChatTask] = []


NotesConfirmResult.model_rebuild()
