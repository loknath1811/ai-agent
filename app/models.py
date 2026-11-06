"""Database tables.

Organization:  Division -> Team -> Employee (Employee.manager_id = direct manager)
Work:          Task (+ TaskEvent history)
Email plumbing: OutboundEmail (what we sent), EmailLog (what we already handled)

All datetimes are naive UTC (see timeutil.py).
"""
import enum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
)
from sqlalchemy.orm import declarative_base, deferred, relationship

from .timeutil import utcnow

Base = declarative_base()


class TaskStatus(str, enum.Enum):
    PENDING = "pending"      # assigned, no reply yet
    ON_TRACK = "on-track"
    BLOCKED = "blocked"
    DELAYED = "delayed"
    DONE = "done"


OPEN_STATUSES = [s.value for s in TaskStatus if s is not TaskStatus.DONE]


class Division(Base):
    __tablename__ = "divisions"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)


class Team(Base):
    __tablename__ = "teams"

    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, unique=True)
    division_id = Column(Integer, ForeignKey("divisions.id"), nullable=True)

    division = relationship("Division", lazy="joined")


class Employee(Base):
    __tablename__ = "employees"

    id = Column(Integer, primary_key=True)
    full_name = Column(String, nullable=False)
    email = Column(String, nullable=False, unique=True, index=True)  # lower-case
    title = Column(String, nullable=True)
    team_id = Column(Integer, ForeignKey("teams.id"), nullable=True)
    manager_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    is_leader = Column(Boolean, nullable=False, default=False)  # may assign work
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=utcnow)

    team = relationship("Team", lazy="joined")


class Task(Base):
    __tablename__ = "tasks"
    # The database itself refuses a second task with the same dedupe_key. This is the last line of defence
    # against two identical requests arriving at the same instant (see services/tasks.py, "idempotency").
    # NULL is allowed many times, so tasks created before this column existed are unaffected.
    __table_args__ = (Index("ux_tasks_dedupe_key", "dedupe_key", unique=True),)

    id = Column(Integer, primary_key=True)
    title = Column(String, nullable=False)
    description = Column(Text, nullable=True)
    status = Column(String, nullable=False, default=TaskStatus.PENDING.value, index=True)

    assignee_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    assigned_by_id = Column(Integer, ForeignKey("employees.id"), nullable=True)
    due_date = Column(DateTime, nullable=True)

    # follow-up bookkeeping
    notified_at = Column(DateTime, nullable=True)        # assignment email sent?
    # assignment-email delivery: waiting -> sent, or waiting -> failed after all retries
    delivery_status = Column(String, nullable=False, default="waiting", server_default="waiting", index=True)
    delivery_attempts = Column(Integer, nullable=False, default=0, server_default="0")
    delivery_error = Column(Text, nullable=True)
    delivery_next_at = Column(DateTime, nullable=True)   # earliest time of the next send attempt
    # "unconfirmed" while the AI was unsure of the status and the employee has been asked to confirm it
    status_check = Column(String, nullable=True)
    last_followup_at = Column(DateTime, nullable=True)
    next_followup_at = Column(DateTime, nullable=True)
    followup_count = Column(Integer, nullable=False, default=0)  # unanswered reminders
    last_reply_at = Column(DateTime, nullable=True)
    # escalation
    escalated = Column(Boolean, nullable=False, default=False)
    escalated_at = Column(DateTime, nullable=True)

    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    # fingerprint of "who asked whom to do what by when, in which time slot" - see services/tasks.py
    dedupe_key = Column(String, nullable=True)

    assignee = relationship("Employee", foreign_keys=[assignee_id], lazy="joined")
    assigned_by = relationship("Employee", foreign_keys=[assigned_by_id], lazy="joined")


class TaskEvent(Base):
    """Audit trail. Every change to a task writes a row here.

    This is the history the proposal wants for throughput / bottleneck analysis.
    event_type: created, delivered, delivery_failed, status_change, reminder_sent,
                escalated, blocker_reported, comment, due_date_changed,
                status_check_asked, status_confirmed, status_corrected, status_check_expired
    """
    __tablename__ = "task_events"

    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False, index=True)
    event_type = Column(String, nullable=False)
    old_status = Column(String, nullable=True)
    new_status = Column(String, nullable=True)
    actor_id = Column(Integer, ForeignKey("employees.id"), nullable=True)  # None = system
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow, index=True)


class OutboundEmail(Base):
    """Every email we send about a task, so replies can be matched by thread."""
    __tablename__ = "outbound_emails"

    message_id = Column(String, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=False, index=True)
    to_email = Column(String, nullable=False)
    kind = Column(String, nullable=False)  # assignment, reminder, escalation, ack ...
    sent_at = Column(DateTime, default=utcnow)


class EmailLog(Base):
    """Inbound emails we've already looked at (so nothing is processed twice).

    status: processed | ignored | failed (failed is retried up to IMAP_MAX_ATTEMPTS)
    """
    __tablename__ = "email_log"

    id = Column(Integer, primary_key=True)
    uid = Column(Integer, nullable=True, index=True)      # IMAP UID
    message_id = Column(String, nullable=True, index=True)
    from_email = Column(String, nullable=True)
    subject = Column(String, nullable=True)
    status = Column(String, nullable=False)
    attempts = Column(Integer, nullable=False, default=1)
    detail = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class Attachment(Base):
    """A file a leader attached in the chat. The bytes live in the database (so they survive hosts with
    temporary disks) and are `deferred`: listing attachments never loads them. Load them on purpose
    with `undefer(Attachment.data)`."""
    __tablename__ = "attachments"

    id = Column(Integer, primary_key=True)
    filename = Column(String, nullable=False)
    content_type = Column(String, nullable=False, default="application/octet-stream")
    size = Column(Integer, nullable=False)
    data = deferred(Column(LargeBinary, nullable=False))
    uploaded_by_id = Column(Integer, ForeignKey("employees.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=utcnow, index=True)


class TaskAttachment(Base):
    """Which files go with which task (one upload can go to several tasks, e.g. a whole team)."""
    __tablename__ = "task_attachments"

    task_id = Column(Integer, ForeignKey("tasks.id"), primary_key=True)
    attachment_id = Column(Integer, ForeignKey("attachments.id"), primary_key=True, index=True)


class OrgChange(Base):
    """Who was added / changed / deactivated in the organization, and when."""
    __tablename__ = "org_changes"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=utcnow, index=True)
    action = Column(String, nullable=False)
    target = Column(String, nullable=True)
    detail = Column(Text, nullable=True)


class RequestLog(Base):
    """Memory of requests we have already acted on, so the SAME request never runs twice.

    Chat: the fingerprint is built from what the leader actually typed. Email: from the email's Message-ID.
    Neither depends on how the AI words its answer, which is why this catches repeats the title/deadline
    check can miss. `key` is the fingerprint plus a time slot and is the PRIMARY KEY, so the database itself
    refuses a second claim made at the same instant. `summary` is what we answered the first time."""
    __tablename__ = "request_log"

    key = Column(String, primary_key=True)
    fingerprint = Column(String, nullable=False, index=True)
    summary = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow, index=True)
