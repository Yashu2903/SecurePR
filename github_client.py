"""
github_client.py — Phase 4, Milestone 1: authenticate as the GitHub App.

Test directly:
    python3 github_client.py <installation_id> <repo_full_name>
"""

import os
import sys

from dotenv import load_dotenv
from github import Auth, GithubIntegration

load_dotenv()

APP_ID = os.environ["GITHUB_APP_ID"]
PRIVATE_KEY_PATH = os.environ["GITHUB_PRIVATE_KEY_PATH"]


def get_installation_client(installation_id: int):
    """Returns a Github client already scoped to one installation —
    it can only see/act on repos that installation was granted access to,
    nothing else."""
    with open(PRIVATE_KEY_PATH, "r") as f:
        private_key = f.read()
    auth = Auth.AppAuth(APP_ID, private_key)
    integration = GithubIntegration(auth=auth)
    return integration.get_github_for_installation(installation_id)


if __name__ == "__main__":
    installation_id = int(sys.argv[1])
    repo_full_name = sys.argv[2]
    gh = get_installation_client(installation_id)
    repo = gh.get_repo(repo_full_name)
    print("Authenticated successfully. Repo:", repo.full_name)