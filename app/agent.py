# app/agent.py

import re
from typing import List, Tuple

from langchain_core.tools import tool
from langchain.agents import create_agent

from .email_client import send_email, fetch_unread, mark_as_read
from .llm_client import get_chat_model
from .crud import create_task, get_task, update_task
from .schemas import TaskCreate, TaskUpdate

# ----------------------------------------------------------------------
# TOOL 1: Assign a new task
# Creates DB record + sends assignment email
# ----------------------------------------------------------------------

@tool
async def assign_task(title: str, assignee_email: str) -> str:
    """
    Create a new task in the database and send an assignment email.
    """

    # Create task in database
    task = await create_task(
        TaskCreate(title=title)
    )

    # Email body
    body = (
        f"Hi,\n\n"
        f"You have been assigned a new task:\n\n"
        f"Title: {title}\n"
        f"Task ID: {task.id}\n\n"
        f"Please reply to this email when you have an update."
    )

    # Send email
    await send_email(
        to=assignee_email,
        subject=f"[Task #{task.id}] New assignment",
        body=body,
    )

    return (
        f"Task {task.id} created and email sent to {assignee_email}"
    )


# ----------------------------------------------------------------------
# TOOL 2: Process incoming emails
# Reads unread emails → classify with LLM → update DB
# ----------------------------------------------------------------------

@tool
async def process_incoming(limit: int = 20) -> str:
    """
    Read unread emails, classify task updates,
    and update the corresponding task status.
    """

    msgs: List[Tuple[int, str, str]] = await fetch_unread(
        limit=limit
    )

    updated = 0
    ignored = 0

    # Prepare LLM model once
    model = get_chat_model()

    for uid, subject, body in msgs:

        # Find Task ID in email subject
        match = re.search(
            r"Task\s*(?:#|ID[:\s]+)(\d+)",
            subject,
            re.IGNORECASE
        )

        if not match:
            ignored += 1
            continue

        task_id = int(match.group(1))

        # Classify email using the configured LLM (async)
        try:
            from langchain_core.messages import HumanMessage, SystemMessage
            system_prompt = (
                "You are a concise status classifier for task-update emails. "
                "Given the email body, reply with exactly one word: "
                "on-track, blocked, delayed, or done."
            )
            response = await model.ainvoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=body),
                ]
            )
            # LangChain returns a dict with a "messages" list; grab last message content
            status_raw = response["messages"][-1].content.strip().lower()
            if status_raw not in {"on-track", "blocked", "delayed", "done"}:
                raise ValueError(f"Unexpected LLM output: {status_raw}")
            status = status_raw
        except Exception:
            ignored += 1
            continue

        # Update task status in database
        await update_task(
            task_id,
            TaskUpdate(status=status)
        )
        await mark_as_read(uid)

        updated += 1

    return (
        f"Processed {len(msgs)} unread messages - "
        f"updated {updated} tasks, ignored {ignored}."
    )


# ----------------------------------------------------------------------
# TOOL 3: Get task status
# ----------------------------------------------------------------------

@tool
async def get_status(task_id: int) -> str:
    """
    Get the current status of a task.
    """

    task = await get_task(task_id)

    return (
        f'Task {task.id} - "{task.title}" -> status: {task.status}'
    )


# ----------------------------------------------------------------------
# CREATE LANGCHAIN AGENT
# ----------------------------------------------------------------------

def get_agent():
    """
    Create and return the LangChain agent.
    """

    # Reuse the configured LLM model
    model = get_chat_model()

    # Tools available to the AI
    tools = [
        assign_task,
        process_incoming,
        get_status,
    ]

    # Create LangChain 1.x agent
    agent = create_agent(
        model=model,
        tools=tools,
        system_prompt=(
            "You are an autonomous task-delegation assistant. "
            "Use the available tools when necessary to complete "
            "the user's request. "
            "Do not pretend an action was completed if a tool "
            "was not successfully called."
        ),
    )

    return agent