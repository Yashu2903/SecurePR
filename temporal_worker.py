"""
temporal_worker.py — Phase 5, Milestone 2: hello-world workflow.

Run:
    python3 temporal_worker.py
"""

import asyncio
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.worker import Worker

TASK_QUEUE = "securepr-task-queue"


@activity.defn
async def say_hello(name: str) -> str:
    return f"Hello, {name}!"


@workflow.defn
class GreetingWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            say_hello, name, start_to_close_timeout=timedelta(seconds=10)
        )


async def main():
    client = await Client.connect("localhost:7233")
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[GreetingWorkflow],
        activities=[say_hello],
    )
    print("Worker started, listening on task queue:", TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())