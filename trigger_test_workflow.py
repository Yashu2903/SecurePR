"""
trigger_test_workflow.py — starts one run of GreetingWorkflow.

Run:
    python3 trigger_test_workflow.py
"""

import asyncio

from temporalio.client import Client

from temporal_worker import GreetingWorkflow, TASK_QUEUE


async def main():
    client = await Client.connect("localhost:7233")
    result = await client.execute_workflow(
        GreetingWorkflow.run,
        "Yashwanth",
        id="test-workflow-1",
        task_queue=TASK_QUEUE,
    )
    print("Workflow result:", result)


if __name__ == "__main__":
    asyncio.run(main())