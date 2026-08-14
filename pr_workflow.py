"""
pr_workflow.py — Phase 10: self-hosted Temporal + in-cluster K8s config.

Activities use in-cluster credentials when running as a real pod (the
actual deployment); falls back to the local kubeconfig for local testing.
"""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from google.cloud import logging_v2
from kubernetes_asyncio import client, config
from temporalio import activity, workflow

from github_client import get_installation_client

GCP_PROJECT_ID = "securepr-505401"
SANDBOX_IMAGE = "us-central1-docker.pkg.dev/securepr-505401/pr-sandbox-repo/pr-sandbox:v2"
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


async def load_k8s_config():
    """Use in-cluster credentials when running as a pod; fall back to the
    local kubeconfig for local testing."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        await config.load_kube_config()


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
        command=["python3"],
        args=["run_pr.py"],
        env=[
            client.V1EnvVar(name="PR_NUMBER", value=str(target.pr_number)),
            client.V1EnvVar(name="PR_REPO_FULL_NAME", value=target.repo_full_name),
            client.V1EnvVar(name="PR_CLONE_URL", value=target.clone_url),
            client.V1EnvVar(name="PR_HEAD_SHA", value=target.head_sha),
        ],
    )
    pod_spec = client.V1PodSpec(
        containers=[container],
        restart_policy="Never",
        runtime_class_name="gvisor",
    )
    template = client.V1PodTemplateSpec(
        metadata=client.V1ObjectMeta(labels={"app": "pr-sandbox"}),
        spec=pod_spec,
    )
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
    await load_k8s_config()
    job = _build_job_manifest(target, check_run_id)
    async with client.ApiClient() as api_client:
        batch_api = client.BatchV1Api(api_client)
        result = await batch_api.create_namespaced_job(namespace=K8S_NAMESPACE, body=job)
    return result.metadata.name


@activity.defn
async def check_job_status_activity(job_name: str) -> str:
    await load_k8s_config()
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
    await load_k8s_config()
    async with client.ApiClient() as api_client:
        core_api = client.CoreV1Api(api_client)
        pods = await core_api.list_namespaced_pod(
            namespace=K8S_NAMESPACE, label_selector=f"job-name={job_name}"
        )
        if not pods.items:
            return "(no pod found)"
        pod_name = pods.items[0].metadata.name

    log_client = logging_v2.Client(project=GCP_PROJECT_ID)
    filter_str = (
        f'resource.type="k8s_container" '
        f'resource.labels.pod_name="{pod_name}" '
        f'resource.labels.namespace_name="{K8S_NAMESPACE}"'
    )

    for _ in range(5):
        entries = list(log_client.list_entries(filter_=filter_str, order_by=logging_v2.ASCENDING))
        if entries:
            return "\n".join(str(e.payload) for e in entries)
        await asyncio.sleep(2)

    return "(no log entries found — Cloud Logging may still be ingesting)"


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
            start_to_close_timeout=timedelta(seconds=60),
        )

        await workflow.execute_activity(
            update_check_run_activity,
            args=[target, check_run_id, status == "succeeded", logs],
            start_to_close_timeout=timedelta(seconds=30),
        )

        return status