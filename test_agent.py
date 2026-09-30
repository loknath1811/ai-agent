import asyncio

from app.agent import get_agent


async def main():
    agent = get_agent()

    result = await agent.ainvoke(
        {
            "messages": [
    {
        "role": "user",
         "content":  "Check the status of task 5"
    }
]
        }
    )

    print(result["messages"][-1].content)


if __name__ == "__main__":
    asyncio.run(main())