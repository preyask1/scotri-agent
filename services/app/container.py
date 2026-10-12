"""Composition root — the ONE place that imports concrete adapters and wires them to ports.

Nothing in domain/ or ports/ imports this module or knows it exists. Nothing outside this module
(and tests) should import a concrete adapter class directly — always go through the container, so
swapping an adapter (env vars -> Key Vault, local Redis -> managed Redis) is a one-line change here.

Every adapter is built LAZILY (functools.cached_property) and only on first access. This matters in
practice, not just in theory: a CLI command that only needs the repo registry (no credentials at
all) must not fail because Databricks/Fabric/Jira/ADO credentials happen to be unconfigured — those
are unrelated concerns that shouldn't be entangled just because they're all "the container."

Adapter modules are also IMPORTED lazily, inside the property that builds them. Importing them all
at module load pulled claude_agent_sdk, mcp and databricks.sdk into every `python -m app.cli` call
— ~5s warm, ~15s cold on Windows — even for a mirror-read that needs none of them. An engagement
makes dozens of CLI calls, so that was minutes of pure startup per investigation (measured
2026-10-01 on the PROJ-2063/PROJ-2085 transcripts). The TYPE_CHECKING block keeps the annotations.
"""
from __future__ import annotations

import logging

from datetime import datetime, timezone
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Callable

from adapters.secrets.env_var_adapter import EnvVarSecretProvider

if TYPE_CHECKING:
    from adapters.adf.rest_run_reader_adapter import AdfRestRunReader
    from adapters.agent_sdk.claude_agent_adapter import ClaudeAgentSdkRuntime
    from adapters.agent_sdk.transcript_reader import ClaudeTranscriptReader
    from adapters.databricks.jobs_run_reader_adapter import DatabricksJobsRunReader
    from adapters.databricks.sdk_adapter import DatabricksSdkQueryExecutor
    from adapters.fabric.rest_semantic_model_adapter import FabricRestSemanticModel
    from adapters.history.sqlite_engagement_history import SqliteEngagementHistory
    from adapters.interaction_log.jsonl_adapter import JsonlInteractionLog
    from adapters.jira.rest_adapter import JiraRestTicketReader
    from adapters.jira.rest_writer_adapter import JiraRestTicketWriter
    from adapters.lineage.sqlite_lineage_graph import SqliteLineageGraph
    from adapters.mirror_sync.git_adapter import GitMirrorSyncTarget
    from adapters.redis.cache_adapter import RedisCache
    from adapters.redis.job_store_adapter import RedisJobStore
    from adapters.redis.maintenance_flag_adapter import RedisMaintenanceFlag
    from adapters.redis.presence_store_adapter import RedisPresenceStore
    from adapters.redis.rate_limiter_adapter import RedisRateLimiter
    from adapters.redis.run_context_adapter import RedisRunContextStore
    from adapters.redis.sync_lock_adapter import RedisSyncLock
    from adapters.registry.yaml_adf_factory_registry import YamlAdfFactoryRegistry
    from adapters.registry.yaml_model_registry_adapter import YamlModelRegistry
    from adapters.registry.yaml_registry_adapter import YamlRepoRegistry
    from adapters.repo_content.live_ado_adapter import LiveAdoRepoContentReader
    from adapters.repo_content.mirror_reader_adapter import MirrorRepoContentReader
    from adapters.retrieval.lexical_retrieval import LexicalRetriever
    from domain.engagement import Engagement
    from domain.shared_files import UploadPolicy
    from ports.artifact_store import ArtifactStore
    from ports.identity_provider import IdentityProvider

SERVICES_ROOT = Path(__file__).resolve().parents[1]


class Container:
    """Lazy composition root. Access an attribute -> that one adapter gets built (and cached);
    everything else stays untouched. Building the container itself never touches Redis, Databricks,
    Fabric, ADO, or Jira — only using a specific property does."""

    def __init__(self, mirror_root: Path | None = None) -> None:
        self.secrets = EnvVarSecretProvider()
        self._mirror_root = mirror_root or self.path_setting("MIRROR_ROOT", SERVICES_ROOT / "mirror")

    def path_setting(self, name: str, default: Path) -> Path:
        """A filesystem path from the environment. A relative value resolves under services/, the
        folder .env lives in, never against the process's working directory (OPEN_ITEMS.md #62): an
        API started from the workspace root used to write a second logs/ folder there, and would
        have opened an empty second history DB and looked for the mirror in the wrong place. So
        `./data/history.db` means the same file whichever directory a process was launched from."""
        value = self.secrets.get_optional(name)
        if not value:
            return default
        path = Path(value).expanduser()
        return path if path.is_absolute() else SERVICES_ROOT / path

    @property
    def mirror_root(self) -> Path:
        return self._mirror_root

    # --- registry: no credentials needed at all -----------------------------------------

    @cached_property
    def client_config(self):
        """The deployment's client settings (config/client.yaml, generated by `app.setup render`):
        enabled optional components, display name, catalogs. Engine defaults when absent."""
        from adapters.registry.yaml_client_config import load_client_config

        return load_client_config()

    @property
    def grounding_catalogs(self) -> tuple[str, ...]:
        """The catalogs whose fully-qualified table names the grounding check verifies: the client
        config's list, else every `catalog` in the repo registry, else none (generic pattern)."""
        if self.client_config.catalogs:
            return self.client_config.catalogs
        try:
            return tuple(sorted({r.catalog for k in self.registry.all_repo_keys()
                                 for r in self.registry.get(k) if r.catalog}))
        except Exception:  # noqa: BLE001 — no registry yet means the generic pattern, not a failure
            return ()

    @cached_property
    def registry(self) -> YamlRepoRegistry:
        from adapters.registry.yaml_registry_adapter import YamlRepoRegistry

        return YamlRepoRegistry()

    @cached_property
    def adf_factories(self) -> YamlAdfFactoryRegistry:
        """Which Azure Data Factory instance (subscription/resource group/factory name) backs the
        pipeline-runs/activity-runs commands, per environment. Separate from `registry` (that one
        resolves git repo/branch for content reads) — see ports/adf_factory_registry.py."""
        from adapters.registry.yaml_adf_factory_registry import YamlAdfFactoryRegistry

        return YamlAdfFactoryRegistry()

    @cached_property
    def lineage_graph(self) -> SqliteLineageGraph:
        """The queryable lineage graph index (OPEN_ITEMS.md item #37). A derived, rebuildable SQLite
        index built from docs/ai/client/lineage-graph.yaml (lineage_source_path) via `lineage-rebuild` — not
        a system of record, so an ephemeral file rebuilt on startup is fine even on AKS."""
        from adapters.lineage.sqlite_lineage_graph import SqliteLineageGraph

        db_path = self.path_setting("LINEAGE_DB_PATH", SERVICES_ROOT / "data" / "lineage.db")
        return SqliteLineageGraph(db_path)

    @property
    def lineage_source_path(self) -> Path:
        """The machine-readable lineage source of truth (docs/ai/client/lineage-graph.yaml at the
        workspace root — SERVICES_ROOT.parent). Overridable via LINEAGE_SOURCE_PATH."""
        return self.path_setting("LINEAGE_SOURCE_PATH",
                                 SERVICES_ROOT.parent / "docs" / "ai" / "client" / "lineage-graph.yaml")

    @cached_property
    def model_registry(self) -> YamlModelRegistry:
        """Which model engagements run on (config/model_registry.yaml). Read by `agent_runtime`;
        one model for every stage since 2026-10-07 (OPEN_ITEMS.md #4)."""
        from adapters.registry.yaml_model_registry_adapter import YamlModelRegistry

        return YamlModelRegistry()

    # --- Redis-backed (needs Redis reachable, no other credentials) --------------------

    @cached_property
    def _redis_client(self):
        from adapters.redis.client import build_redis_client

        return build_redis_client(self.secrets)

    @cached_property
    def rate_limiter(self) -> RedisRateLimiter:
        from adapters.redis.rate_limiter_adapter import RedisRateLimiter

        return RedisRateLimiter(self._redis_client)

    @cached_property
    def cache(self) -> RedisCache:
        from adapters.redis.cache_adapter import RedisCache

        return RedisCache(self._redis_client)

    @cached_property
    def presence_store(self) -> RedisPresenceStore:
        from adapters.redis.presence_store_adapter import RedisPresenceStore

        return RedisPresenceStore(self._redis_client)

    @cached_property
    def maintenance_flag(self) -> RedisMaintenanceFlag:
        from adapters.redis.maintenance_flag_adapter import RedisMaintenanceFlag

        return RedisMaintenanceFlag(self._redis_client)

    @cached_property
    def sync_lock(self) -> RedisSyncLock:
        from adapters.redis.sync_lock_adapter import RedisSyncLock

        return RedisSyncLock(self._redis_client)

    # --- external systems, each needs only its own credentials --------------------------

    @cached_property
    def databricks(self) -> DatabricksSdkQueryExecutor:
        from adapters.databricks.sdk_adapter import DatabricksSdkQueryExecutor

        return DatabricksSdkQueryExecutor(self.secrets)

    @cached_property
    def databricks_runs(self) -> DatabricksJobsRunReader:
        """Databricks job/notebook run-history reads (OPEN_ITEMS.md item #30) — the read-only Jobs
        API 2.1 surface, the third run-history leg alongside adf_runs and fabric refresh history.
        Reuses the SELECT executor's own SP credentials against a different API (Jobs, not SQL), so
        it has no separate credential to configure; a 403 here is a missing Jobs-API grant, not a
        missing credential (see the adapter's docstring)."""
        from adapters.databricks.jobs_run_reader_adapter import DatabricksJobsRunReader

        return DatabricksJobsRunReader(self.secrets)

    @cached_property
    def fabric(self) -> FabricRestSemanticModel:
        from adapters.fabric.rest_semantic_model_adapter import FabricRestSemanticModel

        return FabricRestSemanticModel(self.secrets)

    @cached_property
    def adf_runs(self) -> AdfRestRunReader:
        """ADF pipeline/activity run-history reads (OPEN_ITEMS.md: decided 2026-09-27). Reuses the
        Fabric SP's credentials (decision 1) against a different token scope — see the adapter's
        own docstring — so this has no separate credential of its own to configure."""
        from adapters.adf.rest_run_reader_adapter import AdfRestRunReader

        return AdfRestRunReader(self.secrets)

    @cached_property
    def jira(self) -> JiraRestTicketReader:
        from adapters.jira.rest_adapter import JiraRestTicketReader

        return JiraRestTicketReader(self.secrets)

    @cached_property
    def jira_writer(self) -> JiraRestTicketWriter:
        """Deliberately a separate property from `jira` (same JIRA_EMAIL/JIRA_API_TOKEN account), so the
        read-only intake path is never accidentally entangled with the narrow, confirm-gated write
        exception (jira-write SKILL.md)."""
        from adapters.jira.rest_writer_adapter import JiraRestTicketWriter

        return JiraRestTicketWriter(self.secrets)

    @cached_property
    def mirror_sync(self) -> GitMirrorSyncTarget:
        from adapters.mirror_sync.git_adapter import GitMirrorSyncTarget

        return GitMirrorSyncTarget(self.secrets, self._mirror_root)

    @cached_property
    def mirror_reader(self) -> MirrorRepoContentReader:
        from adapters.repo_content.mirror_reader_adapter import MirrorRepoContentReader

        return MirrorRepoContentReader(self._mirror_root)

    def live_repo_reader(self, org: str, project: str) -> LiveAdoRepoContentReader:
        """Built per-call rather than cached, since org/project vary by repo (see repo_registry.yaml)."""
        from adapters.repo_content.live_ado_adapter import LiveAdoRepoContentReader

        return LiveAdoRepoContentReader(self.secrets, org=org, project=project)

    # --- agent-service (app/api.py) ------------------------------------------------------

    @cached_property
    def identity_provider(self) -> IdentityProvider:
        """SCOTRI_AUTH_MODE=dev swaps in StaticIdentityProvider — no Entra call, no
        AZURE_AD_TENANT_ID/AZURE_AD_API_AUDIENCE needed — for local/test-frontend use only. Any
        other value (including unset) is the real AzureAdIdentityProvider; never defaults to dev."""
        if self.secrets.get_optional("SCOTRI_AUTH_MODE", "prod") == "dev":
            from adapters.dev_auth.static_identity_provider import StaticIdentityProvider

            return StaticIdentityProvider(self.secrets)
        from adapters.azure_ad.token_validator import AzureAdIdentityProvider

        return AzureAdIdentityProvider(self.secrets)

    @cached_property
    def interaction_log(self) -> JsonlInteractionLog:
        from adapters.interaction_log.jsonl_adapter import JsonlInteractionLog

        log_path = self.path_setting("INTERACTION_LOG_PATH", SERVICES_ROOT / "logs" / "interaction-log.jsonl")
        return JsonlInteractionLog(log_path)

    @cached_property
    def engagement_history(self) -> SqliteEngagementHistory:
        """Durable, per-user, per-conversation engagement history (OPEN_ITEMS.md #19/#25). SQLite for
        local/dev; on Azure AKS this swaps to a managed DB (Azure SQL/PostgreSQL) behind the same
        ports.engagement_history.EngagementHistory port — a pod's local disk is ephemeral and not
        shared across replicas, so a file is not the production home for this system of record."""
        from adapters.history.sqlite_engagement_history import SqliteEngagementHistory

        db_path = self.path_setting("ENGAGEMENT_HISTORY_DB_PATH", SERVICES_ROOT / "data" / "history.db")
        return SqliteEngagementHistory(db_path)

    @cached_property
    def caller_registry(self):
        """The admin caller-app register (OPEN_ITEMS.md #43). Same database file as engagement
        history, so it moves with it when #15 picks the production store."""
        from adapters.admin.sqlite_caller_registry import SqliteCallerRegistry

        db_path = self.path_setting("ENGAGEMENT_HISTORY_DB_PATH", SERVICES_ROOT / "data" / "history.db")
        return SqliteCallerRegistry(db_path)

    @cached_property
    def admin_audit_log(self):
        """Every admin action, allowed or refused (OPEN_ITEMS.md #43). Its own file, never mixed into
        the rotating api log."""
        from adapters.admin.jsonl_admin_audit import JsonlAdminAuditLog

        return JsonlAdminAuditLog(self.path_setting("ADMIN_AUDIT_LOG_PATH",
                                                    SERVICES_ROOT / "logs" / "admin-audit.jsonl"))

    @property
    def logs_dir(self) -> Path:
        """Where the entry points write their scotri-*.log files (each passes this same folder to
        configure_logging). Follows `SCOTRI_LOG_DIR` exactly as configure_logging does, so the admin
        log view always reads where the logs really are."""
        return self.path_setting("SCOTRI_LOG_DIR", SERVICES_ROOT / "logs")

    @cached_property
    def retriever(self) -> LexicalRetriever:
        """Lexical (BM25) retrieval over past engagements + docs/ai/ (OPEN_ITEMS.md #38) — a ranker,
        never a candidate generator (see the port/domain docstrings). Reuses engagement_history for
        the past-engagements corpus; docs come from docs/ai/ (SERVICES_ROOT.parent). A hybrid
        embeddings adapter (Foundry) is a later swap behind ports.retrieval.Retriever."""
        from adapters.retrieval.lexical_retrieval import LexicalRetriever

        docs_root = self.path_setting("DOCS_AI_ROOT", SERVICES_ROOT.parent / "docs" / "ai")
        return LexicalRetriever(self.engagement_history, docs_root)

    @cached_property
    def job_store(self) -> RedisJobStore:
        from adapters.redis.job_store_adapter import RedisJobStore

        ttl = int(self.secrets.get_optional("JOB_TTL_SECONDS", str(24 * 60 * 60)))
        return RedisJobStore(self._redis_client, ttl_seconds=ttl)

    @cached_property
    def engagement_queue(self):
        """The admission queue shared by the API and MCP processes (OPEN_ITEMS.md #32,
        domain/engagement_queue.py)."""
        from adapters.redis.engagement_queue_adapter import RedisEngagementQueue

        return RedisEngagementQueue(self._redis_client)

    @cached_property
    def queue_settings(self):
        """Limits for the admission queue: MAX_CONCURRENT_ENGAGEMENTS (3),
        MAX_QUEUED_ENGAGEMENTS_PER_USER (2), ENGAGEMENT_QUEUE_MAX_WAIT_SECONDS (1800),
        ENGAGEMENT_QUEUE_MAX_LENGTH (20)."""
        from domain.engagement_queue import QueueSettings

        return QueueSettings.from_secrets(self.secrets)

    @cached_property
    def progress_store(self):
        """Live engagement progress (OPEN_ITEMS.md #39, domain/engagement_progress.py). Same TTL as the
        job record so the two expire together."""
        from adapters.redis.progress_store_adapter import RedisProgressStore

        ttl = int(self.secrets.get_optional("JOB_TTL_SECONDS", str(24 * 60 * 60)))
        return RedisProgressStore(self._redis_client, ttl_seconds=ttl)

    @cached_property
    def run_context_store(self) -> RedisRunContextStore:
        """Per-run caller identity (OPEN_ITEMS.md #59, domain/run_context.py)."""
        from adapters.redis.run_context_adapter import RedisRunContextStore

        return RedisRunContextStore(self._redis_client)

    @property
    def run_context_ttl_seconds(self) -> int:
        """The run token outlives the engagement's wall-clock ceiling by a margin, so it can't expire
        under a run that is still legitimately working; it is revoked explicitly when the run ends."""
        from adapters.agent_sdk.claude_agent_adapter import ENGAGEMENT_WALL_CLOCK_TIMEOUT_SECONDS

        timeout = float(self.secrets.get_optional(
            "ENGAGEMENT_TIMEOUT_SECONDS", str(ENGAGEMENT_WALL_CLOCK_TIMEOUT_SECONDS)))
        return int(timeout) + 10 * 60

    @cached_property
    def agent_runtime(self) -> ClaudeAgentSdkRuntime:
        """Model resolved from model_registry (config/model_registry.yaml) rather than left unset —
        an unset ClaudeAgentOptions.model omits --model from the CLI call entirely, silently falling
        back to whatever the CLI itself defaults to instead of the registry's model (Claude
        Sonnet 5.5 since 2026-10-07, OPEN_ITEMS.md #4). One engagement is one query() call (no
        per-stage model switch mid-run — see domain/engagement_policy.py), so "default" is the
        only entry that's actually meaningful here."""
        from adapters.agent_sdk.claude_agent_adapter import (
            DEFAULT_MAX_TURNS,
            ENGAGEMENT_WALL_CLOCK_TIMEOUT_SECONDS,
            ClaudeAgentSdkRuntime,
        )

        model_ref = self.model_registry.get("default")
        # Wall-clock ceiling per engagement (OPEN_ITEMS.md #33) — default 20 min, overridable per
        # deployment. Bounds a hung/runaway unattended run; see the adapter constant's docstring.
        timeout_seconds = float(
            self.secrets.get_optional(
                "ENGAGEMENT_TIMEOUT_SECONDS", str(ENGAGEMENT_WALL_CLOCK_TIMEOUT_SECONDS)
            )
        )
        # Turn and cost ceilings (#33): ENGAGEMENT_MAX_TURNS (0 = none), ENGAGEMENT_MAX_BUDGET_USD
        # (unset = none, until real per-run costs say where to put it).
        max_turns = int(self.secrets.get_optional("ENGAGEMENT_MAX_TURNS", str(DEFAULT_MAX_TURNS)))
        budget = (self.secrets.get_optional("ENGAGEMENT_MAX_BUDGET_USD", "") or "").strip()
        return ClaudeAgentSdkRuntime(model=model_ref.model, timeout_seconds=timeout_seconds,
                                     max_turns=max_turns or None, max_budget_usd=float(budget) if budget else None)

    def engagement_prefetch(self, workspace_root: str | Path) -> Callable[[Engagement], str]:
        """The run_engagement `prefetch` hook (domain/engagement_prefetch.py): Jira, presence and
        mirror state fetched in parallel before the agent's first turn. Adapters are passed as
        factories so an unconfigured credential costs one "not fetched" line, not the engagement.
        SCOTRI_PREFETCH=off disables it; SCOTRI_JIRA_PROJECTS (comma-separated, e.g. "PROJ,OPS")
        restricts which key prefixes count as tickets."""
        from domain.engagement_prefetch import PrefetchDeps, build_prefetch_context
        from domain.mirror_freshness import DEFAULT_WARN_AFTER_SECONDS

        if self.secrets.get_optional("SCOTRI_PREFETCH", "on").lower() == "off":
            return lambda engagement: ""
        projects = (self.secrets.get_optional("SCOTRI_JIRA_PROJECTS", "").strip()
                    or ",".join(self.client_config.jira_projects))
        deps = PrefetchDeps(
            ticket_reader=lambda: self.jira,
            presence_store=lambda: self.presence_store,
            rate_limiter=lambda: self.rate_limiter,
            cache=lambda: self.cache,
            maintenance_flag=lambda: self.maintenance_flag,
            registry=self.registry,
            mirror_status=lambda: self.mirror_reader.read_status(),
            scratch_dir=Path(workspace_root) / "scratch",
            stale_warn_seconds=int(self.secrets.get_optional(
                "MIRROR_STALE_WARN_SECONDS", str(DEFAULT_WARN_AFTER_SECONDS))),
            allowed_projects=frozenset(p.strip().upper() for p in projects.split(",") if p.strip()) or None,
            list_tree_files=lambda repo, branch: self.mirror_reader.list_files(repo, branch)[0],
        )

        def prefetch(engagement: Engagement) -> str:
            return build_prefetch_context(deps, engagement.prompt, owner=engagement.principal.subject,
                                          now=datetime.now(timezone.utc), job_id=engagement.job_id)

        return prefetch

    @cached_property
    def artifact_store(self) -> ArtifactStore:
        """Where shared deliverables and uploads live (OPEN_ITEMS.md #6, domain/shared_files.py).
        ARTIFACT_STORE=local (the default, for Mac/Windows) keeps them under ARTIFACT_STORE_DIR
        (services/data/artifacts, gitignored). ARTIFACT_STORE=azure_blob puts them in an Azure Blob
        container shared by every replica, which is the Azure/Foundry setting: a pod's local disk is
        neither durable nor shared. Any other value is refused rather than guessed at."""
        from domain.errors import ConfigurationError

        kind = (self.secrets.get_optional("ARTIFACT_STORE", "local") or "local").lower()
        if kind == "local":
            from adapters.artifact_store.local_fs_adapter import LocalFsArtifactStore

            return LocalFsArtifactStore(self.path_setting("ARTIFACT_STORE_DIR", SERVICES_ROOT / "data" / "artifacts"))
        if kind == "azure_blob":
            from adapters.artifact_store.azure_blob_adapter import AzureBlobArtifactStore

            return AzureBlobArtifactStore(self.secrets)
        raise ConfigurationError(f"ARTIFACT_STORE={kind!r} is not supported; use 'local' or 'azure_blob'")

    @property
    def retention_policy(self):
        """UPLOAD_RETENTION_DAYS / SHARED_FILE_RETENTION_DAYS (OPEN_ITEMS.md #6(a), domain/retention.py).
        Empty or 0 = keep forever, the default."""
        from domain.retention import RetentionPolicy, parse_days

        return RetentionPolicy(
            upload_days=parse_days("UPLOAD_RETENTION_DAYS", self.secrets.get_optional("UPLOAD_RETENTION_DAYS")),
            shared_file_days=parse_days("SHARED_FILE_RETENTION_DAYS",
                                        self.secrets.get_optional("SHARED_FILE_RETENTION_DAYS")),
        )

    @cached_property
    def upload_policy(self) -> UploadPolicy:
        """UPLOAD_MAX_BYTES (default 10 MB) and UPLOAD_ALLOWED_EXTENSIONS (comma-separated, e.g.
        ".md,.csv,.pdf"; default: every format except credential files)."""
        from domain.shared_files import DEFAULT_MAX_UPLOAD_BYTES, DEFAULT_UPLOAD_SUFFIXES, UploadPolicy

        raw = self.secrets.get_optional("UPLOAD_ALLOWED_EXTENSIONS", "") or ""
        suffixes = {("." + s.strip().lstrip(".")).lower() for s in raw.split(",") if s.strip()}
        return UploadPolicy(
            allowed_suffixes=frozenset(suffixes) if suffixes else DEFAULT_UPLOAD_SUFFIXES,
            max_bytes=int(self.secrets.get_optional("UPLOAD_MAX_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES))),
        )

    def role_set(self, name: str) -> set[str]:
        """A comma-separated role list from the environment, e.g. HISTORY_ADMIN_ROLES=lead,admin."""
        return {r.strip() for r in (self.secrets.get_optional(name, "") or "").split(",") if r.strip()}

    def release_run_claims(self, job_id: str, owner: str) -> list[str]:
        """Release every presence claim engagement `job_id` registered that `owner` still holds, and
        forget the record. Returns what was released. A claim someone else took over is left alone."""
        store = self.presence_store
        released = []
        for entity in store.run_claims(job_id):
            claim = store.get_claim(entity)
            if claim is not None and claim.owner == owner:
                store.release_claim(entity, owner)
                released.append(entity)
        store.forget_run(job_id)
        return released

    @property
    def grounding_evidence(self):
        """The grounding check's evidence source (OPEN_ITEMS.md #68, domain/grounding.py): the run's own
        transcript. On unless GROUNDING_CHECK=off. Transcripts are host-local, like /steps (#61)."""
        if (self.secrets.get_optional("GROUNDING_CHECK", "on") or "on").strip().lower() == "off":
            return None
        return self.trace_reader.read_turn_evidence

    @cached_property
    def mirror_text_cache(self):
        """mirror-grep's per-tree text cache (OPEN_ITEMS.md #46, adapters/repo_content/text_cache.py):
        MIRROR_TEXT_CACHE_DIR, default services/data/mirror-text-cache (gitignored). Rebuilt after
        each sync, never written into the mirror itself."""
        from adapters.repo_content.text_cache import MirrorTextCache

        return MirrorTextCache(self.path_setting("MIRROR_TEXT_CACHE_DIR", SERVICES_ROOT / "data" / "mirror-text-cache"))

    def rebuild_mirror_text_cache(self, ref) -> None:
        """The sync's post-swap step (OPEN_ITEMS.md #46): rebuild the freshly swapped tree's
        mirror-grep cache, so no engagement pays for it on its first search."""
        from concurrent.futures import ThreadPoolExecutor

        from adapters.repo_content.text_cache import ensure_index

        with ThreadPoolExecutor(max_workers=8) as pool:
            index = ensure_index(self.mirror_text_cache, self.mirror_reader, ref.repo, ref.branch, pool.map)
        logging.getLogger(__name__).info("sync: %s@%s search cache %s", ref.repo, ref.branch,
                                         f"ready ({len(index['paths'])} files)" if index else "skipped (no manifest)")

    @cached_property
    def trace_reader(self) -> ClaudeTranscriptReader:
        """Where the Agent SDK's CLI writes session transcripts — `~/.claude` unless
        CLAUDE_CONFIG_DIR (which the CLI itself honors) points elsewhere. Only finds transcripts on
        the host the engagement ran on; see OPEN_ITEMS.md if the API ever runs on more than one."""
        from adapters.agent_sdk.transcript_reader import ClaudeTranscriptReader

        config_dir = self.secrets.get_optional("CLAUDE_CONFIG_DIR", str(Path.home() / ".claude"))
        return ClaudeTranscriptReader(Path(config_dir).expanduser())


def build_container(mirror_root: Path | None = None) -> Container:
    """Kept as a function (rather than having every call site do `Container()` directly) so the
    handful of places that build a container all read the same way, and so this remains the one
    obvious place to add cross-cutting setup later (e.g. logging config) if that's ever needed."""
    return Container(mirror_root=mirror_root)
