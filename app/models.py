import datetime
import enum

from sqlalchemy import Column, Integer, String, DateTime
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class TaskStatus(str, enum.Enum):
    PENDING = "pending"
    ON_TRACK = "on-track"
    BLOCKED = "blocked"
    DELAYED = "delayed"
    DONE = "done"


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String, nullable=False)

    status = Column(
        String,
        nullable=False,
        default="pending"
    )

    created_at = Column(
        DateTime,
        default=datetime.datetime.utcnow
    )

    updated_at = Column(
        DateTime,
        default=datetime.datetime.utcnow,
        onupdate=datetime.datetime.utcnow
    )