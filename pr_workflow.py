"""
pr_workflow.py — Phase 5, Milestone 3: PR verification as a Temporal workflow.

Each activity wraps exactly one external call (GitHub API or Kubernetes
API). The workflow just sequences them — this replaces Phase 4's
asyncio.create_task poller with something Temporal itself tracks and
can resume even if our process restarts.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kubernetes_asyncio import client, config
from temporalio import activity, workflow

from github_client import get_installation_client

SANDBOX_IMAGE = "pr-sandbox:v2"
K8S_NAMESPACE = "default"
CHECK_RUN_NAME = "SecurePRBox Sandbox Execution"
TASK_QUEUE = "securepr-task-queue"


@dataclass
class PRTarget:
    repo_full_name: str
    clone_url: str
    pr_number: int
    head_sha: str
    head_ref: str
    base_ref: str
    installation_id: int


# ---- Activities ----------------------------------------------------

@activity.defn
async def create_check_run_activity(target: PRTarget) -> int:
    gh = get_installation_client(target.installation_id)
    repo = gh.get_repo(target.repo_full_name)
    check_run = repo.create_check_run(
        name=CHECK_RUN_NAME,
        head_sha=target.head_sha,
        status="in_progress",
    )
    return check_run.id


def _build_job_manifest(target: PRTarget, check_run_id: int) -> client.V1Job:
    container = client.V1Container(
        name="sandbox",
        image=SANDBOX_IMAGE,
        image_pull_policy="Never",
        security_context=client.V1SecurityContext(
            capabilities=client.V1Capabilities(add=["SYS_ADMIN"])
        ),
        env=[
            client.V1EnvVar(name="PR_NUMBER", value=str(target.pr_number)),
            client.V1EnvVar(name="PR_REPO_FULL_NAME", value=target.repo_full_name),
            client.V1EnvVar(name="PR_CLONE_URL", value=target.clone_url),
            client.V1EnvVar(name="PR_HEAD_SHA", value=target.head_sha),
        ],
    )
    pod_spec = client.V1PodSpec(containers=[container], restart_policy="Never")
    template = client.V1PodTemplateSpec(spec=pod_spec)
    job_spec = client.V1JobSpec(template=template, backoff_limit=0)
    return client.V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=client.V1ObjectMeta(
            generate_name=f"pr-{target.pr_number}-",
            annotations={
                "securepr/check-run-id": str(check_run_id),
                "securepr/installation-id": str(target.installation_id),
                "securepr/repo-full-name": target.repo_full_name,
            },
        ),
        spec=job_spec,
    )


@activity.defn
async def create_sandbox_job_activity(target: PRTarget, check_run_id: int) -> str:
    await config.load_kube_config()
    job = _build_job_manifest(target, check_run_id)
    async with client.ApiClient() as api_client:
        batch_api = client.BatchV1Api(api_client)
        result = await batch_api.create_namespaced_job(namespace=K8S_NAMESPACE, body=job)
    return result.metadata.name


@activity.defn
async def check_job_status_activity(job_name: str) -> str:
    """Returns 'running', 'succeeded', or 'failed'. One check per call —
    the workflow does the waiting/looping, not this activity."""
    await config.load_kube_config()
    async with client.ApiClient() as api_client:
        batch_api = client.BatchV1Api(api_client)
        job = await batch_api.read_namespaced_job_status(name=job_name, namespace=K8S_NAMESPACE)
        if job.status.succeeded:
            return "succeeded"
        if job.status.failed:
            return "failed"
        return "running"


@activity.defn
async def get_job_logs_activity(job_name: str) -> str:
    await config.load_kube_config()
    async with client.ApiClient() as api_client:
        core_api = client.CoreV1Api(api_client)
        pods = await core_api.list_namespaced_pod(
            namespace=K8S_NAMESPACE, label_selector=f"job-name={job_name}"
        )
        if not pods.items:
            return "(no pod found)"
        pod_name = pods.items[0].metadata.name
        return await core_api.read_namespaced_pod_log(name=pod_name, namespace=K8S_NAMESPACE)


@activity.defn
async def update_check_run_activity(target: PRTarget, check_run_id: int, succeeded: bool, logs: str) -> None:
    gh = get_installation_client(target.installation_id)
    repo = gh.get_repo(target.repo_full_name)
    check_run = repo.get_check_run(check_run_id)
    check_run.edit(
        status="completed",
        conclusion="success" if succeeded else "failure",
        completed_at=datetime.now(timezone.utc),
        output={
            "title": "Sandbox execution result",
            "summary": f"```\n{logs[:3000]}\n```",
        },
    )


# ---- Workflow --------------------------------------------------------

@workflow.defn
class PRVerificationWorkflow:
    @workflow.run
    async def run(self, target: PRTarget) -> str:
        check_run_id = await workflow.execute_activity(
            create_check_run_activity, target,
            start_to_close_timeout=timedelta(seconds=30),
        )

        job_name = await workflow.execute_activity(
            create_sandbox_job_activity, args=[target, check_run_id],
            start_to_close_timeout=timedelta(seconds=30),
        )

        while True:
            status = await workflow.execute_activity(
                check_job_status_activity, job_name,
                start_to_close_timeout=timedelta(seconds=10),
            )
            if status != "running":
                break
            await workflow.sleep(timedelta(seconds=3))

        logs = await workflow.execute_activity(
            get_job_logs_activity, job_name,
            start_to_close_timeout=timedelta(seconds=30),
        )

        await workflow.execute_activity(
            update_check_run_activity,
            args=[target, check_run_id, status == "succeeded", logs],
            start_to_close_timeout=timedelta(seconds=30),
        )

        return status