"""
pr_workflow.py — Phase 15: the risk engine.

Risk scoring is fully deterministic — never the AI's call, same principle
as the execution planner in Phase 13. Gemini only ever writes the
plain-English explanation of a verdict that's already been decided by
fixed rules.
"""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from google import genai
from google.genai import types
from google.cloud import logging_v2
from kubernetes_asyncio import client, config
from temporalio import activity, workflow

from github_client import get_installation_client

GCP_PROJECT_ID = "securepr-505401"
GCP_LOCATION = "us-central1"
PLANNER_MODEL = "gemini-2.5-pro"
SANDBOX_IMAGE = "us-central1-docker.pkg.dev/securepr-505401/pr-sandbox-repo/pr-sandbox:v9"
K8S_NAMESPACE = "default"
CHECK_RUN_NAME = "SecurePRBox Sandbox Execution"
TASK_QUEUE = "securepr-task-queue"

DETERMINISTIC_MARKERS = {"securepr.sh", "package.json", "requirements.txt", "pyproject.toml"}


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


@activity.defn
async def plan_execution_activity(target: PRTarget) -> dict:
    """If the repo has a recognized marker file, no AI call happens at
    all — return an empty plan and let the sandbox's existing
    deterministic detection handle it. Only ask Gemini when nothing
    deterministic matched. Scoped to Node.js and Python only for now —
    other languages will typically fail cleanly (no toolchain, no
    allowlisted registry), which is an acceptable, honest outcome."""
    gh = get_installation_client(target.installation_id)
    repo = gh.get_repo(target.repo_full_name)
    contents = repo.get_contents("", ref=target.head_sha)
    filenames = [c.name for c in contents]

    if any(name in DETERMINISTIC_MARKERS for name in filenames):
        return {"install_cmd": "", "test_cmd": ""}

    readme_text = ""
    for candidate in ("README.md", "README", "readme.md"):
        if candidate in filenames:
            readme_file = repo.get_contents(candidate, ref=target.head_sha)
            readme_text = readme_file.decoded_content.decode("utf-8", errors="ignore")[:2000]
            break

    readme_context = f"README excerpt:\n{readme_text}" if readme_text else "No README found."

    prompt = f"""A GitHub repository has this root-level file listing:
{filenames}

{readme_context}

This sandbox can only run Node.js (npm) or Python (pip) projects — no
other language toolchains are installed, and network access is limited
to github.com, registry.npmjs.org, pypi.org, and files.pythonhosted.org.

Propose a single shell install command and a single shell test command,
using only npm or pip. If you cannot confidently determine this is a
Node.js or Python project, return empty strings for both rather than
guessing."""

    gemini_client = genai.Client(vertexai=True, project=GCP_PROJECT_ID, location=GCP_LOCATION)
    response = gemini_client.models.generate_content(
        model=PLANNER_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema={
                "type": "object",
                "properties": {
                    "install_cmd": {"type": "string"},
                    "test_cmd": {"type": "string"},
                },
                "required": ["install_cmd", "test_cmd"],
            },
        ),
    )
    return json.loads(response.text)


def _build_job_manifest(target: PRTarget, check_run_id: int, plan: dict) -> client.V1Job:
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
            client.V1EnvVar(name="PLAN_INSTALL_CMD", value=plan.get("install_cmd", "")),
            client.V1EnvVar(name="PLAN_TEST_CMD", value=plan.get("test_cmd", "")),
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
async def create_sandbox_job_activity(target: PRTarget, check_run_id: int, plan: dict) -> str:
    await load_k8s_config()
    job = _build_job_manifest(target, check_run_id, plan)
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

    # Cloud Logging doesn't reliably preserve relative order between
    # entries ingested very close together in time, so wait specifically
    # for the one self-contained line we know marks completion, rather
    # than trusting overall order.
    marker = "FINDINGS_JSON:"
    entries = []
    for _ in range(15):
        entries = list(log_client.list_entries(filter_=filter_str, order_by=logging_v2.ASCENDING))
        payloads = [str(e.payload) for e in entries]
        if any(p.startswith(marker) for p in payloads):
            return "\n".join(payloads)
        await asyncio.sleep(3)

    return "\n".join(str(e.payload) for e in entries) if entries else "(no log entries found)"


@activity.defn
async def assess_risk_activity(logs: str, build_succeeded: bool) -> dict:
    """Risk scoring is fully deterministic — never the AI's call. Gemini
    only writes the plain-English explanation of a verdict that's
    already decided by the rules below."""
    findings = {"semgrep_count": 0, "trivy_total_count": 0,
                "trivy_critical_count": 0, "trivy_high_count": 0, "gitleaks_count": 0}
    marker = "FINDINGS_JSON:"
    for line in logs.splitlines():
        if line.startswith(marker):
            try:
                findings = json.loads(line[len(marker):])
            except json.JSONDecodeError:
                pass
            break

    if not build_succeeded:
        conclusion = "failure"
    elif findings["gitleaks_count"] > 0 or findings["trivy_critical_count"] > 0:
        conclusion = "failure"
    elif findings["trivy_high_count"] > 0:
        conclusion = "neutral"
    else:
        conclusion = "success"

    # Give the AI the tail of the actual output too, so it can explain
    # *why* something failed, not just restate that it did. Grab the
    # text right before our own findings marker — that's reliably where
    # a real failure summary (pytest's, npm's, whatever) shows up.
    marker_pos = logs.find("FINDINGS_JSON:")
    if marker_pos > 0:
        log_excerpt = logs[max(0, marker_pos - 2000):marker_pos]
    else:
        log_excerpt = logs[-2000:]

    prompt = f"""A PR's automated checks produced these results:
- Build/test: {"passed" if build_succeeded else "failed"}
- Semgrep findings: {findings['semgrep_count']}
- Trivy vulnerabilities: {findings['trivy_total_count']} total ({findings['trivy_critical_count']} critical, {findings['trivy_high_count']} high)
- Gitleaks (potential secrets): {findings['gitleaks_count']}
- Overall verdict already decided: {conclusion}

The tail of the actual execution log:
{log_excerpt}

Write a 2-3 sentence plain-English summary of these results for a
developer reviewing this PR. If the build/test failed, explain the
actual reason based on the log above, not just that it failed. Be
direct about anything serious. Do not propose a different verdict
than the one given."""

    gemini_client = genai.Client(vertexai=True, project=GCP_PROJECT_ID, location=GCP_LOCATION)
    response = gemini_client.models.generate_content(model=PLANNER_MODEL, contents=prompt)

    return {"conclusion": conclusion, "summary": response.text}


@activity.defn
async def update_check_run_activity(target: PRTarget, check_run_id: int, conclusion: str, summary: str, logs: str) -> None:
    gh = get_installation_client(target.installation_id)
    repo = gh.get_repo(target.repo_full_name)
    check_run = repo.get_check_run(check_run_id)
    check_run.edit(
        status="completed",
        conclusion=conclusion,
        completed_at=datetime.now(timezone.utc),
        output={
            "title": "Sandbox execution result",
            "summary": summary,
            "text": f"```\n{logs[:3000]}\n```",
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

        plan = await workflow.execute_activity(
            plan_execution_activity, target,
            start_to_close_timeout=timedelta(seconds=60),
        )

        job_name = await workflow.execute_activity(
            create_sandbox_job_activity, args=[target, check_run_id, plan],
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

        risk = await workflow.execute_activity(
            assess_risk_activity, args=[logs, status == "succeeded"],
            start_to_close_timeout=timedelta(seconds=60),
        )

        await workflow.execute_activity(
            update_check_run_activity,
            args=[target, check_run_id, risk["conclusion"], risk["summary"], logs],
            start_to_close_timeout=timedelta(seconds=30),
        )

        return risk["conclusion"]
