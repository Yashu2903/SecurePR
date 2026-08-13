#!/usr/bin/env python3
"""
run_pr.py — clones the PR's code at its exact commit and runs it.

Convention: if the repo has a `securepr.sh` at its root, run it and use
its exit code as the real result. Otherwise, just prove the clone worked.
"""

import os
import subprocess
import sys

CLONE_URL = os.environ["PR_CLONE_URL"]
HEAD_SHA = os.environ["PR_HEAD_SHA"]
REPO_DIR = "/workspace/repo"


def run(cmd, **kwargs):
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, **kwargs)


def main():
    clone = run(["git", "clone", CLONE_URL, REPO_DIR])
    if clone.returncode != 0:
        print("Failed to clone the repository.")
        sys.exit(1)

    checkout = run(["git", "checkout", HEAD_SHA], cwd=REPO_DIR)
    if checkout.returncode != 0:
        print("Failed to checkout the PR's commit.")
        sys.exit(1)

    script_path = os.path.join(REPO_DIR, "securepr.sh")
    if os.path.isfile(script_path):
        print("Found securepr.sh — running it.")
        result = run(["bash", "securepr.sh"], cwd=REPO_DIR)
        sys.exit(result.returncode)

    print("No securepr.sh found. Listing repo contents instead:")
    run(["ls", "-la"], cwd=REPO_DIR)
    print("Clone and checkout succeeded. Nothing to run yet.")
    sys.exit(0)


if __name__ == "__main__":
    main()