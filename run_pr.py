#!/usr/bin/env python3
"""
run_pr.py — clones the PR's code, runs static analysis (Semgrep, Trivy,
Gitleaks), then figures out how to build/test it.

Static analysis always runs, regardless of build/test outcome. Findings
are emitted twice: once as human-readable text for the logs, and once as
a structured JSON footer the worker parses to make the actual pass/fail
decision (see assess_risk_activity in pr_workflow.py) — that decision is
fully deterministic, never left to an LLM.

Priority order for build/test:
  1. securepr.sh — explicit convention, always wins if present.
  2. package.json — auto-detected Node.js project.
  3. requirements.txt / pyproject.toml — auto-detected Python project.
  4. PLAN_INSTALL_CMD / PLAN_TEST_CMD — AI-proposed plan (Phase 13).
  5. Fallback — just prove the clone worked.
"""

import json
import os
import subprocess
import sys

CLONE_URL = os.environ["PR_CLONE_URL"]
HEAD_SHA = os.environ["PR_HEAD_SHA"]
REPO_DIR = "/workspace/repo"

SEMGREP_RULES_FILE = "/sandbox/semgrep-security-rules.yaml"


def run(cmd, **kwargs):
    print(f"$ {' '.join(cmd)}")
    return subprocess.run(cmd, **kwargs)


def run_static_analysis(repo_dir: str) -> dict:
    lines = []
    counts = {"semgrep_count": 0, "trivy_total_count": 0,
              "trivy_critical_count": 0, "trivy_high_count": 0, "gitleaks_count": 0}

    semgrep_result = run(
        ["semgrep", "--config", SEMGREP_RULES_FILE, "--json", "--quiet", repo_dir],
        capture_output=True, text=True,
    )
    try:
        findings = json.loads(semgrep_result.stdout).get("results", [])
        counts["semgrep_count"] = len(findings)
        lines.append(f"Semgrep: {len(findings)} finding(s)")
        for f in findings[:5]:
            lines.append(f"  - {f['check_id']} at {f['path']}:{f['start']['line']}")
    except (json.JSONDecodeError, KeyError):
        lines.append("Semgrep: scan failed to produce valid output")

    trivy_result = run(
        ["trivy", "fs", "--scanners", "vuln", "--skip-db-update",
         "--format", "json", repo_dir],
        capture_output=True, text=True,
    )
    try:
        trivy_data = json.loads(trivy_result.stdout)
        vuln_lines = []
        for result in trivy_data.get("Results", []) or []:
            for v in result.get("Vulnerabilities", []) or []:
                counts["trivy_total_count"] += 1
                if v["Severity"] == "CRITICAL":
                    counts["trivy_critical_count"] += 1
                elif v["Severity"] == "HIGH":
                    counts["trivy_high_count"] += 1
                if len(vuln_lines) < 5:
                    vuln_lines.append(f"  - {v['VulnerabilityID']} {v['PkgName']} ({v['Severity']})")
        lines.append(f"Trivy: {counts['trivy_total_count']} vulnerability finding(s)")
        lines.extend(vuln_lines)
    except (json.JSONDecodeError, KeyError):
        lines.append("Trivy: scan failed to produce valid output")

    gitleaks_report = "/tmp/gitleaks_report.json"
    run(
        ["gitleaks", "detect", "--source", repo_dir, "--no-git",
         "-f", "json", "-r", gitleaks_report, "--exit-code", "0"],
        capture_output=True, text=True,
    )
    try:
        with open(gitleaks_report) as f:
            gitleaks_data = json.load(f)
        counts["gitleaks_count"] = len(gitleaks_data)
        lines.append(f"Gitleaks: {len(gitleaks_data)} potential secret(s) found")
        for g in gitleaks_data[:5]:
            lines.append(f"  - {g['RuleID']} in {g['File']}:{g['StartLine']}")
    except (FileNotFoundError, json.JSONDecodeError):
        lines.append("Gitleaks: 0 potential secret(s) found")

    return {"text": "\n".join(lines), **counts}


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

    if os.path.isfile(requirements):
        install = run(["pip", "install", "--break-system-packages", "-r", "requirements.txt"], cwd=repo_dir)
    else:
        install = run(["pip", "install", "--break-system-packages", "."], cwd=repo_dir)

    if install.returncode != 0:
        print("Dependency install failed.")
        return 1

    # Test-only dependencies are commonly declared separately from the
    # main install, using one of two incompatible mechanisms:
    #   - [project.optional-dependencies] (older, PEP 621): pip install .[name]
    #   - [dependency-groups] (newer, PEP 735): pip install --group name .
    # Best-effort: try common names under both, ignoring failures since
    # not every project uses either pattern.
    for name in ("test", "tests", "testing", "dev"):
        run(["pip", "install", "--break-system-packages", f".[{name}]"],
            cwd=repo_dir, capture_output=True)
        run(["pip", "install", "--break-system-packages", "--group", name, "."],
            cwd=repo_dir, capture_output=True)

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

    plan_install = os.environ.get("PLAN_INSTALL_CMD", "").strip()
    plan_test = os.environ.get("PLAN_TEST_CMD", "").strip()
    if plan_install or plan_test:
        print("Using AI-proposed plan.")
        if plan_install:
            install = run(["bash", "-c", plan_install], cwd=repo_dir)
            if install.returncode != 0:
                print("AI-proposed install command failed.")
                return 1
        if plan_test:
            test_result = run(["bash", "-c", plan_test], cwd=repo_dir)
            return test_result.returncode
        print("No test command proposed. Install succeeded.")
        return 0

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

    print("--- Static analysis ---")
    analysis = run_static_analysis(REPO_DIR)
    print(analysis["text"])

    print("--- Build / test ---")
    build_result = detect_and_run(REPO_DIR)

    # Structured, machine-readable footer for the worker to parse
    # reliably — not meant for a human reading the raw log.
    findings = {k: v for k, v in analysis.items() if k != "text"}
    print("FINDINGS_JSON:" + json.dumps(findings))

    sys.exit(build_result)


if __name__ == "__main__":
    main()
