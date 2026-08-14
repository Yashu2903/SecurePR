#!/usr/bin/env python3
"""
run_pr.py — clones the PR's code at its exact commit and figures out how
to build/test it.

Priority order:
  1. securepr.sh — explicit convention, always wins if present.
  2. package.json — auto-detected Node.js project.
  3. (Python detection added in the next milestone.)
  4. Fallback — just prove the clone worked.
"""

import json
import os
import subprocess
import sys

CLONE_URL = os.environ["PR_CLONE_URL"]
HEAD_SHA = os.environ["PR_HEAD_SHA"]
REPO_DIR = "/workspace/repo"


def run(cmd, **kwargs):
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, **kwargs)


def run_node_project(repo_dir: str, package_json_path: str) -> int:
    print("Detected package.json — Node.js project.")
    install = run(["npm", "install"], cwd=repo_dir)
    if install.returncode != 0:
        print("npm install failed.")
        return 1

    with open(package_json_path) as f:
        package = json.load(f)
    has_test_script = "test" in package.get("scripts", {})

    if not has_test_script:
        print("No test script defined in package.json. Install succeeded, nothing to test.")
        return 0

    print("Running npm test.")
    test_result = run(["npm", "test"], cwd=repo_dir)
    return test_result.returncode

def run_python_project(repo_dir: str) -> int:
    print("Detected a Python project.")
    requirements = os.path.join(repo_dir, "requirements.txt")
    pyproject = os.path.join(repo_dir, "pyproject.toml")

    if os.path.isfile(requirements):
        install = run(["pip", "install", "--break-system-packages", "-r", "requirements.txt"], cwd=repo_dir)
    else:
        install = run(["pip", "install", "--break-system-packages", "."], cwd=repo_dir)

    if install.returncode != 0:
        print("Dependency install failed.")
        return 1

    has_tests = os.path.isdir(os.path.join(repo_dir, "tests")) or any(
        f.startswith("test_") and f.endswith(".py") for f in os.listdir(repo_dir)
    )
    if not has_tests:
        print("No tests found. Install succeeded, nothing to test.")
        return 0

    print("Running pytest.")
    test_result = run(["pytest"], cwd=repo_dir)
    return test_result.returncode


def detect_and_run(repo_dir: str) -> int:
    securepr_sh = os.path.join(repo_dir, "securepr.sh")
    if os.path.isfile(securepr_sh):
        print("Found securepr.sh — running it.")
        result = run(["bash", "securepr.sh"], cwd=repo_dir)
        return result.returncode

    package_json = os.path.join(repo_dir, "package.json")
    if os.path.isfile(package_json):
        return run_node_project(repo_dir, package_json)

    requirements = os.path.join(repo_dir, "requirements.txt")
    pyproject = os.path.join(repo_dir, "pyproject.toml")
    if os.path.isfile(requirements) or os.path.isfile(pyproject):
        return run_python_project(repo_dir)

    print("No recognized build system found. Listing repo contents instead:")
    run(["ls", "-la"], cwd=repo_dir)
    print("Clone and checkout succeeded. Nothing to run.")
    return 0


def main():
    clone = run(["git", "clone", CLONE_URL, REPO_DIR])
    if clone.returncode != 0:
        print("Failed to clone the repository.")
        sys.exit(1)

    checkout = run(["git", "checkout", HEAD_SHA], cwd=REPO_DIR)
    if checkout.returncode != 0:
        print("Failed to checkout the PR's commit.")
        sys.exit(1)

    sys.exit(detect_and_run(REPO_DIR))


if __name__ == "__main__":
    main()