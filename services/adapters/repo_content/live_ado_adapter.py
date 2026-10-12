"""Azure DevOps Git REST adapter — the on-demand, never-mirrored Dev/UAT read path (fabric-read /
adf-databricks-read SKILL.md, Path B). A live network call every time, deliberately not cached to
disk: Dev content changes while someone's actively working on it, and persisting a copy would
recreate the local-clone dependency this workspace's architecture avoids.

Uses a READ-ONLY PAT only — see agent-instructions.md: no write-capable git credential is ever
configured for this agent.
"""
from __future__ import annotations

import requests

from adapters.ado_auth import resolve_ado_pat
from ports.secret_provider import SecretProvider

_API_VERSION = "7.1"


def _normalize_path(path: str) -> str:
    """Callers (CLI args, ultimately from a skill's Bash-tool invocation) should never need to
    pass a leading "/" themselves — Git Bash/MSYS silently rewrites a bare "/" (or any argument
    that LOOKS like an absolute POSIX path) into a Windows filesystem path before Python ever sees
    it. Confirmed 2026-09-18. So the convention is: callers pass "" for root or a path with no
    leading slash ("Sales/Model.SemanticModel/..."), and this function adds the "/" ADO's API actually wants,
    internally, where no shell can mangle it."""
    stripped = path.lstrip("/\\")
    return "/" + stripped if stripped else "/"


class LiveAdoRepoContentReader:
    """Implements ports.repo_content.RepoContentReader, reading live over ADO Git REST."""

    def __init__(self, secrets: SecretProvider, org: str, project: str) -> None:
        self._pat = resolve_ado_pat(secrets, org)
        self._org = org
        self._project = project

    def _auth(self) -> tuple[str, str]:
        # ADO Git REST accepts Basic auth with an empty username and the PAT as the password.
        return ("", self._pat)

    def _base_url(self, repo: str) -> str:
        return (
            f"https://dev.azure.com/{self._org}/{self._project}/_apis/git/"
            f"repositories/{repo}/items"
        )

    def list_dir(self, repo: str, branch: str, path: str) -> list[str]:
        resp = requests.get(
            self._base_url(repo),
            params={
                "scopePath": _normalize_path(path),
                "recursionLevel": "OneLevel",
                "versionDescriptor.version": branch,
                "api-version": _API_VERSION,
            },
            auth=self._auth(),
            timeout=20,
        )
        resp.raise_for_status()
        entries = resp.json().get("value", [])
        return [e["path"].rsplit("/", 1)[-1] for e in entries]

    def read_file(self, repo: str, branch: str, path: str) -> str:
        resp = requests.get(
            self._base_url(repo),
            params={
                "path": _normalize_path(path),
                "versionDescriptor.version": branch,
                "api-version": _API_VERSION,
                "$format": "text",
            },
            auth=self._auth(),
            timeout=20,
        )
        resp.raise_for_status()
        return resp.text
