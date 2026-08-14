"""
github_client.py — Phase 10, Milestone 2: reads secrets from Secret Manager
instead of a local private key file.
"""

from github import Auth, GithubIntegration
from google.cloud import secretmanager

GCP_PROJECT_ID = "securepr-505401"
APP_ID = "4562657"


def get_secret(secret_id: str) -> str:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{GCP_PROJECT_ID}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(name=name)
    return response.payload.data.decode("UTF-8")


def get_installation_client(installation_id: int):
    private_key = get_secret("github-app-private-key")
    auth = Auth.AppAuth(APP_ID, private_key)
    integration = GithubIntegration(auth=auth)
    return integration.get_github_for_installation(installation_id)


if __name__ == "__main__":
    import sys
    installation_id = int(sys.argv[1])
    repo_full_name = sys.argv[2]
    gh = get_installation_client(installation_id)
    repo = gh.get_repo(repo_full_name)
    print("Authenticated successfully. Repo:", repo.full_name)