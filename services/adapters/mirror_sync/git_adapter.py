"""Git-based mirror sync — the sync service's own adapter, the ONLY place in this whole codebase
that touches mirror-staging/ or writes anything under the mirror root at all.

Every other adapter (mirror_reader_adapter) only ever reads mirror-active/. This module is the one
exception, by design (agent-instructions.md: "the sync service, not this agent, refreshes the
mirror").

Uses a read-only PAT embedded in the clone URL for auth — never a write-scoped credential; there is
no `git push` anywhere in this module. The PAT is resolved PER ORG, per call (see adapters/ado_auth.py).

SECURITY: every subprocess call in this module goes through `_run_git` (or `_run_git_progress`, its
streaming twin for the admin panel's progress bars), which re-raise any failure as a redacted
`GitSyncError` — `from None`, so Python's default exception chaining can't smuggle the original
(PAT-bearing) exception back into a traceback or a log line. `_run_git_progress` passes its observer
only a stage name and a number, never a line of git's output. A real PAT leaked into a log once during
development (2026-09-17) before this existed; that PAT was rotated. Never bypass these two with a raw
`subprocess` call in this file.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import stat
import subprocess
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from adapters.ado_auth import resolve_ado_pat
from adapters.repo_content.mirror_reader_adapter import walk_tree
from domain.mirror_text import MANIFEST_NAME
from domain.errors import ErrorCategory, ScotriError, TransientError
from ports.secret_provider import SecretProvider

logger = logging.getLogger(__name__)

_REDACTED = "***REDACTED***"


def _win_long_path(path: Path) -> str:
    """The `\\\\?\\` prefix bypasses Windows' 260-character MAX_PATH limit for file APIs. Applied
    to every rmtree call — confirmed 2026-09-17: a Fabric repo's deeply-nested folder names
    (e.g. `.../StaticResources/RegisteredResources/<long-generated-name>.json`) exceed MAX_PATH
    and Python's own os.unlink/os.rmdir hit it too, not just git's checkout."""
    if os.name == "nt":
        resolved = str(path.resolve())
        return resolved if resolved.startswith("\\\\?\\") else "\\\\?\\" + resolved
    return str(path)


def _rmtree_force(path: Path) -> None:
    """shutil.rmtree, but clears the read-only attribute git sets on pack files before retrying,
    and uses the long-path-safe form of the path (see _win_long_path).

    Confirmed 2026-09-17: plain shutil.rmtree fails with WinError 5 (Access Denied) on
    .git/objects/pack/*.idx — git marks these read-only on Windows as a data-integrity measure,
    and shutil.rmtree doesn't clear that attribute on its own.
    """
    def _on_error(func, target_path, exc):  # noqa: ARG001 — exc unused, signature required by rmtree
        os.chmod(target_path, stat.S_IWRITE)
        func(target_path)

    shutil.rmtree(_win_long_path(path), onexc=_on_error)

# This agent runs headless — "no human at a keyboard" (agent-instructions.md). Without this, a
# credential rejection can make Git (or Git Credential Manager on Windows) fall back to an
# interactive prompt, which hangs forever with no one there to answer it. Confirmed 2026-09-17:
# a bad-credential test against a real ADO host hung for exactly this reason.
_NO_PROMPT_ENV = {
    **os.environ,
    "GIT_TERMINAL_PROMPT": "0",
    "GCM_INTERACTIVE": "never",
    "GIT_ASKPASS": "echo",
}


# git's progress lines ("Receiving objects:  45% (450/1000)"), mapped to FETCH_STAGES names. Only the
# stage and the number are ever passed on; the line itself is dropped.
_PROGRESS = re.compile(r"(Receiving objects|Resolving deltas|Updating files|Checking out files):\s+(\d{1,3})%")
_STAGE = {"Receiving objects": "receiving", "Resolving deltas": "resolving",
          "Updating files": "checkout", "Checking out files": "checkout"}

ProgressCallback = Callable[[str, float], None]


class GitSyncError(ScotriError):
    """Raised instead of subprocess.CalledProcessError — message is pre-redacted of any secret.

    Category is inferred from the redacted stderr text — best-effort, not exhaustive, but enough
    to tell an admin "this needs a credential fixed" apart from "this is probably transient."
    """


def _redact(text: str, *secrets: str) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, _REDACTED)
    return text


def _categorize_git_failure(stderr: str) -> ErrorCategory:
    lowered = stderr.lower()
    if "authentication failed" in lowered or "could not read username" in lowered:
        return ErrorCategory.CREDENTIAL
    if "timed out" in lowered or "connection timed out" in lowered:
        return ErrorCategory.TIMEOUT
    if "could not resolve host" in lowered or "connection reset" in lowered or "recv failure" in lowered:
        return ErrorCategory.TRANSIENT
    return ErrorCategory.UNKNOWN


class GitMirrorSyncTarget:
    """Implements ports.mirror_sync.MirrorSyncTarget."""

    def __init__(self, secrets: SecretProvider, mirror_root: Path) -> None:
        self._secrets = secrets
        self._staging_root = mirror_root / "mirror-staging"
        self._active_root = mirror_root / "mirror-active"
        self._staging_root.mkdir(parents=True, exist_ok=True)
        self._active_root.mkdir(parents=True, exist_ok=True)

    def _clone_url(self, org: str, project: str, repo: str) -> tuple[str, str]:
        """Returns (url, pat) — caller must pass `pat` to `_run_git`'s `redact` so it never leaks."""
        pat = resolve_ado_pat(self._secrets, org)
        return f"https://{pat}@dev.azure.com/{org}/{project}/_git/{repo}", pat

    def _staging_path(self, repo: str, branch: str) -> Path:
        return self._staging_root / repo / branch

    def _active_path(self, repo: str, branch: str) -> Path:
        return self._active_root / repo / branch

    @staticmethod
    def _run_git(args: list[str], *, redact: tuple[str, ...] = (), timeout: float = 120) -> None:
        try:
            subprocess.run(
                args, check=True, capture_output=True, text=True,
                env=_NO_PROMPT_ENV, timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            safe_cmd = [_redact(a, *redact) for a in exc.cmd]
            raise GitSyncError(
                f"git command timed out after {timeout}s: {safe_cmd}",
                category=ErrorCategory.TIMEOUT,
            ) from None
        except subprocess.CalledProcessError as exc:
            safe_cmd = [_redact(a, *redact) for a in exc.cmd]
            safe_stderr = _redact(exc.stderr or "", *redact)
            safe_stdout = _redact(exc.stdout or "", *redact)
            raise GitSyncError(
                f"git command failed (exit {exc.returncode}): {safe_cmd}\n"
                f"stderr: {safe_stderr}\nstdout: {safe_stdout}",
                category=_categorize_git_failure(safe_stderr),
            ) from None  # `from None`: suppress chaining so the original (PAT-bearing) exception
            # can never be printed by a traceback or by logger.exception() further up the stack.

    @staticmethod
    def _run_git_progress(args: list[str], on_progress: ProgressCallback, *, redact: tuple[str, ...] = (),
                          timeout: float = 120) -> None:
        """`_run_git`, but streams stderr so git's `--progress` percentages reach `on_progress` as they
        happen. The same redaction applies to every error; the callback only ever sees a stage name
        and a number parsed out of a line, never the line."""
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=_NO_PROMPT_ENV)
        err_chunks: list[bytes] = []
        out_chunks: list[bytes] = []

        def pump_stderr() -> None:
            buf = b""
            for block in iter(lambda: proc.stderr.read1(4096), b""):
                err_chunks.append(block)
                *lines, buf = re.split(rb"[\r\n]", buf + block)
                for line in lines:
                    m = _PROGRESS.search(line.decode("utf-8", "replace"))
                    if m:
                        try:
                            on_progress(_STAGE[m.group(1)], float(m.group(2)))
                        except Exception:  # noqa: BLE001 — a broken observer must not break the sync
                            logger.debug("sync: progress callback failed", exc_info=True)

        def pump_stdout() -> None:
            for block in iter(lambda: proc.stdout.read1(4096), b""):
                out_chunks.append(block)

        readers = [threading.Thread(target=pump_stderr, name="git-progress", daemon=True),
                   threading.Thread(target=pump_stdout, name="git-stdout", daemon=True)]
        for r in readers:
            r.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            safe_cmd = [_redact(a, *redact) for a in args]
            raise GitSyncError(
                f"git command timed out after {timeout}s: {safe_cmd}",
                category=ErrorCategory.TIMEOUT,
            ) from None
        finally:
            for r in readers:
                r.join(5)
        if proc.returncode != 0:
            safe_cmd = [_redact(a, *redact) for a in args]
            safe_stderr = _redact(b"".join(err_chunks).decode("utf-8", "replace"), *redact)
            safe_stdout = _redact(b"".join(out_chunks).decode("utf-8", "replace"), *redact)
            raise GitSyncError(
                f"git command failed (exit {proc.returncode}): {safe_cmd}\n"
                f"stderr: {safe_stderr}\nstdout: {safe_stdout}",
                category=_categorize_git_failure(safe_stderr),
            ) from None

    def _git(self, args: list[str], on_progress: ProgressCallback | None, redact: tuple[str, ...]) -> None:
        if on_progress is None:
            self._run_git(args, redact=redact)
        else:
            self._run_git_progress(args, on_progress, redact=redact)

    def fetch_into_staging(self, repo: str, branch: str, org: str, project: str,
                           on_progress: ProgressCallback | None = None) -> None:
        """Incremental fetch + reset, not a full re-clone, once a staging clone already exists.

        Every call disables the credential helper (`-c credential.helper=`) so Windows Git
        Credential Manager never intercepts the URL-embedded PAT — confirmed 2026-09-17: GCM's own
        credential resolution produced an inconsistent, hard-to-debug failure ("Cannot determine
        the organization name...") on one branch of a repo while an identical clone of a different
        branch of the SAME repo succeeded, which pointed straight at GCM getting involved rather
        than the PAT itself being wrong. This flag makes auth behavior deterministic: the
        URL-embedded PAT is the only credential source, always.
        """
        # core.longpaths=true: Fabric repos nest Semantic Model / Report folders deep enough to
        # exceed Windows' default 260-character path limit — confirmed 2026-09-17, a Fabric repo's
        # checkout failed partway through with "Filename too long" without this. Applied to every
        # command below, not just clone, since a reset --hard checks files out too.
        staging_path = self._staging_path(repo, branch)
        if staging_path.exists():
            # No credential in these two commands' own argv — they use the existing 'origin'
            # remote already configured at clone time — but redact defensively anyway in case a
            # future git version ever echoes the remote URL into an error message.
            _, pat = self._clone_url(org, project, repo)
            self._git(
                ["git", "-c", "credential.helper=", "-c", "core.longpaths=true",
                 "-C", str(staging_path), "fetch", "--depth", "1"]
                + (["--progress"] if on_progress else []) + ["origin", branch],
                on_progress, (pat,),
            )
            self._git(
                ["git", "-c", "credential.helper=", "-c", "core.longpaths=true",
                 "-C", str(staging_path), "reset", "--hard", "origin/" + branch],
                on_progress, (pat,),
            )
        else:
            staging_path.parent.mkdir(parents=True, exist_ok=True)
            url, pat = self._clone_url(org, project, repo)
            self._git(
                ["git", "-c", "credential.helper=", "-c", "core.longpaths=true",
                 "clone", "--depth", "1"] + (["--progress"] if on_progress else [])
                + ["--branch", branch, url, str(staging_path)],
                on_progress, (pat,),
            )
        if on_progress:
            on_progress("indexing", 0)
        _write_manifest(staging_path, repo, branch)
        if on_progress:
            on_progress("indexing", 100)

    def swap_to_active(self, repo: str, branch: str) -> None:
        """Atomic rename: staging becomes active. Same-filesystem rename is atomic on both
        Windows and POSIX; if mirror_root ever spans filesystems, this needs revisiting."""
        staging_path = self._staging_path(repo, branch)
        active_path = self._active_path(repo, branch)
        active_path.parent.mkdir(parents=True, exist_ok=True)

        previous_backup = active_path.with_name(active_path.name + ".previous")
        if active_path.exists():
            if previous_backup.exists():
                _rmtree_force(previous_backup)
            _rename_with_retry(active_path, previous_backup)
        try:
            _rename_with_retry(staging_path, active_path)
        except Exception:
            # Swap failed — restore the previous active tree rather than leaving nothing in place.
            if previous_backup.exists():
                _rename_with_retry(previous_backup, active_path)
            raise
        else:
            if previous_backup.exists():
                _rmtree_force(previous_backup)


_RENAME_DELAYS = (0.5, 1, 2, 4, 8)
"""Seconds to wait before each retry of a swap rename (~15 s in all). On Windows a folder can't be
renamed while any process holds a file inside it open: a mirror reader mid-scan, antivirus or the
search indexer. Those holds are usually brief; the 2026-10-12 06:25 sync lost two trees to one."""


def _rename_with_retry(src: Path, dst: Path, delays=_RENAME_DELAYS, sleep=None) -> None:
    """`src.rename(dst)`, retried on a Windows sharing/access error (a file inside is open
    elsewhere). Anything else, or a lock that outlasts the retries, raises: the latter as a
    TransientError that says what holds it, rather than an uncategorised OSError."""
    import time

    sleep = sleep or time.sleep
    for attempt, delay in enumerate((*delays, None)):
        try:
            src.rename(dst)
            return
        except PermissionError as e:  # WinError 5 (access denied) / 32 (in use)
            if delay is None:
                raise TransientError(
                    f"could not rename {src.name} -> {dst} after {attempt + 1} attempts: a file inside is "
                    f"held open by another process (a mirror reader, antivirus or the search indexer). "
                    f"The previous tree is kept; re-run the sync once it is released. ({e})") from e
            logger.warning("sync: rename %s -> %s blocked (%s); retrying in %ss", src, dst, e, delay)
            sleep(delay)


def _write_manifest(tree: Path, repo: str, branch: str) -> None:
    """The file list mirror-find/-grep read instead of walking the tree on every call. Written into
    staging, so the swap moves it into mirror-active/ together with the files it lists — a reader
    never sees a manifest describing a different tree. Best-effort: if it can't be written the sync
    still succeeds, and the reader falls back to walking the tree."""
    try:
        files = walk_tree(tree)
        payload = {"repo": repo, "branch": branch, "generated_at": datetime.now(timezone.utc).isoformat(),
                   "file_count": len(files), "files": files}
        (tree / MANIFEST_NAME).write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        logger.warning("sync: could not write the file manifest for %s@%s; readers will walk the tree",
                       repo, branch, exc_info=True)
