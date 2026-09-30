from sqlalchemy import select, update
from fastapi import HTTPException, status

from .database import async_session
from .models import Task
from .schemas import TaskCreate, TaskUpdate


async def create_task(task_in: TaskCreate) -> Task:
    async with async_session() as session:
        async with session.begin():
            task = Task(
                title=task_in.title,
                status="pending"
            )
            session.add(task)

        await session.commit()
        await session.refresh(task)

        return task


async def get_task(task_id: int) -> Task:
    async with async_session() as session:
        result = await session.execute(
            select(Task).where(Task.id == task_id)
        )

        task = result.scalar_one_or_none()

        if not task:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Task {task_id} not found"
            )

        return task


async def list_tasks() -> list[Task]:
    async with async_session() as session:
        result = await session.execute(
            select(Task).order_by(Task.id.desc())
        )

        return result.scalars().all()


async def update_task(
    task_id: int,
    task_in: TaskUpdate
) -> Task:

    async with async_session() as session:
        async with session.begin():

            values = {
                k: v
                for k, v in task_in.model_dump().items()
                if v is not None
            }

            stmt = (
                update(Task)
                .where(Task.id == task_id)
                .values(**values)
                .execution_options(
                    synchronize_session="fetch"
                )
            )

            result = await session.execute(stmt)

            if result.rowcount == 0:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Task {task_id} not found"
                )

        await session.commit()

    return await get_task(task_id)