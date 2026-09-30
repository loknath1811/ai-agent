from fastapi import APIRouter
from pydantic import BaseModel

from .agent import get_agent


router = APIRouter(
    prefix="/agent",
    tags=["Agent"]
)


class AgentRequest(BaseModel):
    message: str


class AgentResponse(BaseModel):
    response: str


@router.post("", response_model=AgentResponse)
async def run_agent(request: AgentRequest):

    agent = get_agent()

    result = await agent.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": request.message
                }
            ]
        }
    )

    response = result["messages"][-1].content

    if isinstance(response, list):
        response = "".join(
            block.get("text", "")
            for block in response
            if isinstance(block, dict)
        )

    return AgentResponse(
        response=response
    )


@router.post("/process-emails")
async def process_emails():

    agent = get_agent()

    result = await agent.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "Process incoming emails"
                }
            ]
        }
    )

    response = result["messages"][-1].content

    if isinstance(response, list):
        response = "".join(
            block.get("text", "")
            for block in response
            if isinstance(block, dict)
        )

    return {
        "response": response
    }