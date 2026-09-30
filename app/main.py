from contextlib import asynccontextmanager

from fastapi import FastAPI, status

from dotenv import load_dotenv

load_dotenv()
from .schemas import TaskCreate, TaskRead, TaskUpdate
from .crud import create_task, get_task, list_tasks, update_task
from .routes_agent import router as agent_router
from .scheduler import start_scheduler, shutdown_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Runs once when the server starts
    start_scheduler()
    yield
    # Runs once when the server stops
    await shutdown_scheduler()


app = FastAPI(
    title="AI Work-Delegation Agent",
    lifespan=lifespan,
)


# ---------------------------------------------------------
# AGENT ROUTES
# ---------------------------------------------------------

app.include_router(agent_router)


# ---------------------------------------------------------
# HEALTH CHECK
# ---------------------------------------------------------

@app.get("/health")
async def health_check() -> dict:
    return {
        "status": "ok"
    }


# ---------------------------------------------------------
# TASK ROUTES
# ---------------------------------------------------------

@app.post(
    "/tasks",
    response_model=TaskRead,
    status_code=status.HTTP_201_CREATED
)
async def api_create_task(task: TaskCreate):
    return await create_task(task)


@app.get(
    "/tasks",
    response_model=list[TaskRead]
)
async def api_list_tasks():
    return await list_tasks()


@app.get(
    "/tasks/{task_id}",
    response_model=TaskRead
)
async def api_get_task(task_id: int):
    return await get_task(task_id)


@app.patch(
    "/tasks/{task_id}",
    response_model=TaskRead
)
async def api_update_task(
    task_id: int,
    payload: TaskUpdate
):
    return await update_task(task_id, payload)