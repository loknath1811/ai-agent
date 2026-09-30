from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from .models import TaskStatus


class TaskBase(BaseModel):
    title: str = Field(
        ..., example="Ask Ravi to finish the report by Friday"
    )


class TaskCreate(TaskBase):
    pass


class TaskRead(TaskBase):
    model_config = ConfigDict(from_attributes=True)

    id: int
    status: TaskStatus
    created_at: datetime
    updated_at: datetime


class TaskUpdate(BaseModel):
    model_config = ConfigDict(use_enum_values=True)

    status: Optional[TaskStatus] = Field(
        None,
        description="One of: pending, on-track, blocked, delayed, done"
    )