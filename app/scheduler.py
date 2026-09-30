from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .agent import process_incoming


scheduler = AsyncIOScheduler()


async def process_emails_job():
    print("Checking for new task emails...")

    try:
        result = await process_incoming.ainvoke(
            {
                "limit": 20
            }
        )

        print(f"Email processing result: {result}")

    except Exception as e:
        print(f"Email processing failed: {e}")


def start_scheduler():
    scheduler.add_job(
        process_emails_job,
        "interval",
        minutes=5,
        id="process_task_emails",
        replace_existing=True,
    )

    scheduler.start()

    print("Email scheduler started.")


async def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        print("Email scheduler stopped.")