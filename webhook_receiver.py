"""
webhook_receiver.py — Phase 10, Milestone 4: deployable to the cluster.

Run locally:
    uvicorn webhook_receiver:app --reload --port 8000
"""

import hashlib
import hmac
import json
import logging

from fastapi import FastAPI, HTTPException, Request
from temporalio.client import Client

from config import TEMPORAL_ADDRESS
from github_client import get_secret
from pr_workflow import PRTarget, PRVerificationWorkflow, TASK_QUEUE

WEBHOOK_SECRET = get_secret("webhook-secret")

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("webhook")

app = FastAPI()

RELEVANT_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


def extract_pull_request_target(payload: dict) -> PRTarget:
    pr = payload["pull_request"]
    return PRTarget(
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


@app.on_event("startup")
async def startup_event():
    app.state.temporal_client = await Client.connect(TEMPORAL_ADDRESS)


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
    workflow_id = f"pr-{target.repo_full_name}-{target.pr_number}-{target.head_sha[:7]}"

    handle = await app.state.temporal_client.start_workflow(
        PRVerificationWorkflow.run,
        target,
        id=workflow_id,
        task_queue=TASK_QUEUE,
    )
    log.info("Started workflow %s for PR #%d (%s)", workflow_id, target.pr_number, target.repo_full_name)
    return {"status": "workflow_started", "workflow_id": handle.id}


@app.get("/healthz")
async def health():
    return {"status": "ok"}