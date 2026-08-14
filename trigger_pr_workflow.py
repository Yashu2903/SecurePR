"""
trigger_pr_workflow.py — manually starts one real PR verification run.

Run:
    python3 trigger_pr_workflow.py
"""

import asyncio
import time
from temporalio.client import Client

from pr_workflow import PRTarget, PRVerificationWorkflow, TASK_QUEUE


async def main():
    target = PRTarget(
        repo_full_name="Yashu2903/securepr_test",
        clone_url="https://github.com/Yashu2903/securepr_test.git",
        pr_number=2,
        head_sha="380390dec51535cb090595c1283796d1714e3f2c",
        head_ref="yashwanth",
        base_ref="main",
        installation_id=153019133,
    )
    client = await Client.connect("localhost:7233")
    result = await client.execute_workflow(
        PRVerificationWorkflow.run,
        target,
        id=f"test-pr-workflow-{int(time.time())}",
        task_queue=TASK_QUEUE,
    )
    print("Workflow result:", result)


if __name__ == "__main__":
    asyncio.run(main())