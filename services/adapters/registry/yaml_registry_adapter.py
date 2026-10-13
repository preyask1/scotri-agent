# Copyright (c) 2026 Preyas Kulshrestha. Part of the Scotri engine; MIT License (see LICENSE).
# Keep this notice in copies and derived works, including AI-assisted ones.
"""Loads config/repo_registry.yaml and implements the RepoRegistry port.

This is the one place YAML parsing happens — nothing in domain/ or ports/ knows the registry is a
YAML file at all, which is exactly what lets the sync-to-docs/ai/client/repo-registry.md hand-maintained
duplication (see that file's own header) become a database or an API call later without touching
anything upstream of this adapter.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from domain.errors import ConfigurationError
from domain.models import Environment, RepoRef

DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[2] / "config" / "repo_registry.yaml"


class YamlRepoRegistry:
    """Implements ports.repo_registry.RepoRegistry."""

    def __init__(self, path: Path = DEFAULT_REGISTRY_PATH) -> None:
        self._path = path
        self._by_key: dict[str, list[RepoRef]] = {}
        self._load()

    def _load(self) -> None:
        if not Path(self._path).is_file():
            raise ConfigurationError(
                f"{self._path} does not exist. It is generated from client/profile.yaml: run "
                f"`python -m app.setup render` from services/ (see SETUP.md)."
            )
        with open(self._path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)

        for repo_entry in data.get("repos", []):
            repo_key = repo_entry["repo_key"]
            default_ado_repo = repo_entry.get("ado_repo")
            ado_org = repo_entry["ado_org"]
            ado_project = repo_entry["ado_project"]
            repo_status = repo_entry.get("status")

            refs: list[RepoRef] = []
            for env_entry in repo_entry["environments"]:
                ado_repo = env_entry.get("ado_repo", default_ado_repo)
                if ado_repo is None:
                    raise ValueError(
                        f"repo_registry.yaml: '{repo_key}' has no ado_repo at either the repo "
                        f"or environment level for environment '{env_entry['environment']}'"
                    )
                refs.append(
                    RepoRef(
                        repo=ado_repo,
                        org=ado_org,
                        project=ado_project,
                        branch=env_entry["branch"],
                        environment=Environment(env_entry["environment"]),
                        mirrored=bool(env_entry["mirrored"]),
                        catalog=env_entry.get("catalog"),
                        status=repo_status,
                    )
                )
            self._by_key[repo_key] = refs

    # --- RepoRegistry port -------------------------------------------------------------

    def get(self, repo_key: str) -> list[RepoRef]:
        return list(self._by_key.get(repo_key, []))

    def all_repo_keys(self) -> list[str]:
        return list(self._by_key.keys())
