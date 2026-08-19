"""
temporal_worker.py — Phase 15: registers the risk assessment activity.

Run:
    python3 temporal_worker.py
"""

import asyncio
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from config import TEMPORAL_ADDRESS
from pr_workflow import (
    TASK_QUEUE,
    PRVerificationWorkflow,
    create_check_run_activity,
    plan_execution_activity,
    create_sandbox_job_activity,
    check_job_status_activity,
    get_job_logs_activity,
    assess_risk_activity,
    update_check_run_activity,
)

sandbox_runner = SandboxedWorkflowRunner(
    restrictions=SandboxRestrictions.default.with_passthrough_modules(
        "kubernetes_asyncio", "urllib3", "github", "aiohttp", "google"
    )
)


async def main():
    client = await Client.connect(TEMPORAL_ADDRESS)
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[PRVerificationWorkflow],
        activities=[
            create_check_run_activity,
            plan_execution_activity,
            create_sandbox_job_activity,
            check_job_status_activity,
            get_job_logs_activity,
            assess_risk_activity,
            update_check_run_activity,
        ],
        workflow_runner=sandbox_runner,
    )
    print("Worker started, listening on task queue:", TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())