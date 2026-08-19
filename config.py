"""
config.py — single source of truth for every identifier that used to
be duplicated or hardcoded across multiple files. Each value can be
overridden via environment variable in any deployment, without touching
code — but every default matches the current real deployment, so
nothing breaks if the env var isn't set.
"""

import os

# --- GCP ---
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "securepr-505401")
GCP_LOCATION = os.environ.get("GCP_LOCATION", "us-central1")

# --- GitHub App ---
GITHUB_APP_ID = os.environ.get("GITHUB_APP_ID", "4562657")

# --- Container images ---
SANDBOX_IMAGE = os.environ.get(
    "SANDBOX_IMAGE",
    "us-central1-docker.pkg.dev/securepr-505401/pr-sandbox-repo/pr-sandbox:v11",
)

# --- Kubernetes ---
K8S_NAMESPACE = os.environ.get("K8S_NAMESPACE", "default")

# --- GitHub Checks ---
CHECK_RUN_NAME = os.environ.get("CHECK_RUN_NAME", "SecurePRBox Sandbox Execution")

# --- Temporal ---
TASK_QUEUE = os.environ.get("TASK_QUEUE", "securepr-task-queue")
TEMPORAL_ADDRESS = os.environ.get("TEMPORAL_ADDRESS", "localhost:7233")

# --- AI ---
PLANNER_MODEL = os.environ.get("PLANNER_MODEL", "gemini-2.5-pro")
