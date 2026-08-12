"""
webhook_receiver.py — Phase 4, Milestone 2: create an in-progress Check Run.

Run:
    uvicorn webhook_receiver:app --reload --port 8000
"""

import hashlib
import hmac
import json
import logging
import os
import asyncio
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from kubernetes_asyncio import client, config
from pydantic import BaseModel
from datetime import datetime, timezone

from github_client import get_installation_client

load_dotenv()

WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
SANDBOX_IMAGE = "pr-sandbox:v1"
K8S_NAMESPACE = "default"
CHECK_RUN_NAME = "SecurePRBox Sandbox Execution"

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhook")

app = FastAPI()

RELEVANT_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


class PullRequestTarget(BaseModel):
    action: str
    repo_full_name: str
    clone_url: str
    pr_number: int
    head_sha: str
    head_ref: str
    base_ref: str
    installation_id: int


def extract_pull_request_target(payload: dict) -> PullRequestTarget:
    pr = payload["pull_request"]
    return PullRequestTarget(
        action=payload["action"],
        repo_full_name=payload["repository"]["full_name"],
        clone_url=payload["repository"]["clone_url"],
        pr_number=payload["number"],
        head_sha=pr["head"]["sha"],
        head_ref=pr["head"]["ref"],
        base_ref=pr["base"]["ref"],
        installation_id=payload["installation"]["id"],
    )


def verify_signature(raw_body: bytes, signature_header: str) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def create_in_progress_check_run(target: PullRequestTarget) -> int:
    """Posts the initial 'checks running' state to the PR. Returns the
    Check Run's id so it can be updated later once the Job finishes."""
    gh = get_installation_client(target.installation_id)
    repo = gh.get_repo(target.repo_full_name)
    check_run = repo.create_check_run(
        name=CHECK_RUN_NAME,
        head_sha=target.head_sha,
        status="in_progress",
    )
    return check_run.id


def build_job_manifest(target: PullRequestTarget, check_run_id: int) -> client.V1Job:
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
        args=["--", "/bin/bash", "-c",
              'echo "PR number $PR_NUMBER on $PR_REPO_FULL_NAME at $PR_HEAD_SHA"; hostname'],
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


async def create_sandbox_job(target: PullRequestTarget, check_run_id: int) -> str:
    job = build_job_manifest(target, check_run_id)
    async with client.ApiClient() as api_client:
        batch_api = client.BatchV1Api(api_client)
        result = await batch_api.create_namespaced_job(namespace=K8S_NAMESPACE, body=job)
    return result.metadata.name

async def watch_job_completion(job_name: str, check_run_id: int) -> None:
    """Polls until the Job finishes, then logs the outcome. Milestone 4
    will replace the logging with an actual Check Run update."""
    async with client.ApiClient() as api_client:
        batch_api = client.BatchV1Api(api_client)
        while True:
            job = await batch_api.read_namespaced_job_status(name=job_name, namespace=K8S_NAMESPACE)
            if job.status.succeeded:
                log.info("Job %s succeeded (check_run_id=%s)", job_name, check_run_id)
                return
            if job.status.failed:
                log.info("Job %s failed (check_run_id=%s)", job_name, check_run_id)
                return
            await asyncio.sleep(3)

async def get_job_pod_logs(job_name: str) -> str:
    async with client.ApiClient() as api_client:
        core_api = client.CoreV1Api(api_client)
        pods = await core_api.list_namespaced_pod(
            namespace=K8S_NAMESPACE, label_selector=f"job-name={job_name}"
        )
        if not pods.items:
            return "(no pod found)"
        pod_name = pods.items[0].metadata.name
        return await core_api.read_namespaced_pod_log(name=pod_name, namespace=K8S_NAMESPACE)


def update_check_run(target: PullRequestTarget, check_run_id: int, succeeded: bool, logs: str) -> None:
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


async def watch_job_completion(job_name: str, check_run_id: int, target: PullRequestTarget) -> None:
    """Polls until the Job finishes, then updates the Check Run with the
    real outcome and the container's logs."""
    async with client.ApiClient() as api_client:
        batch_api = client.BatchV1Api(api_client)
        while True:
            job = await batch_api.read_namespaced_job_status(name=job_name, namespace=K8S_NAMESPACE)
            if job.status.succeeded or job.status.failed:
                succeeded = bool(job.status.succeeded)
                logs = await get_job_pod_logs(job_name)
                update_check_run(target, check_run_id, succeeded, logs)
                log.info("Check Run %s updated: %s", check_run_id, "success" if succeeded else "failure")
                return
            await asyncio.sleep(3)


@app.on_event("startup")
async def startup_event():
    await config.load_kube_config()


@app.post("/webhook")
async def receive_webhook(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("X-Hub-Signature-256", "")

    if not verify_signature(raw_body, signature):
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(raw_body)
    except Exception:
        raise HTTPException(status_code=400, detail="Body was empty or not valid JSON")

    event_type = request.headers.get("X-GitHub-Event", "unknown")

    if event_type != "pull_request" or payload.get("action") not in RELEVANT_ACTIONS:
        log.info("Ignored event=%s action=%s", event_type, payload.get("action"))
        return {"status": "ignored"}

    target = extract_pull_request_target(payload)
    check_run_id = create_in_progress_check_run(target)
    job_name = await create_sandbox_job(target, check_run_id)
    asyncio.create_task(watch_job_completion(job_name, check_run_id, target))
    log.info("Created Job %s + Check Run %s for PR #%d (%s)",
              job_name, check_run_id, target.pr_number, target.repo_full_name)
    return {"status": "job_created", "job_name": job_name, "check_run_id": check_run_id}


@app.get("/healthz")
async def health():
    return {"status": "ok"}