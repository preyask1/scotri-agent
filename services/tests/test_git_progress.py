"""adapters/mirror_sync/git_adapter.py's streaming runner: git's --progress percentages reach the
observer as (stage, percent) and nothing else, failures stay redacted, and a hung git is killed."""
from __future__ import annotations

import shutil
import subprocess
import sys

import pytest

from adapters.mirror_sync.git_adapter import GitMirrorSyncTarget, GitSyncError
from domain.errors import ErrorCategory

run = GitMirrorSyncTarget._run_git_progress


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_progress_lines_become_stage_and_percent_only():
    seen = []
    script = ("import sys\n"
              "sys.stderr.write('remote: Counting objects: 10\\n')\n"
              "sys.stderr.write('Receiving objects:  10% (1/10)\\rReceiving objects: 100% (10/10), done.\\n')\n"
              "sys.stderr.write('Resolving deltas:  50% (1/2)\\r')\n"
              "sys.stderr.write('Updating files: 100% (3/3), done.\\n')\n")
    run(_py(script), lambda stage, pct: seen.append((stage, pct)))
    assert seen == [("receiving", 10.0), ("receiving", 100.0), ("resolving", 50.0), ("checkout", 100.0)]


def test_a_failure_is_redacted_and_categorized():
    secret = "pat-0123456789abcdef"
    script = f"import sys; sys.stderr.write('fatal: Authentication failed for https://{secret}@dev.azure.com/x\\n'); sys.exit(128)"
    with pytest.raises(GitSyncError) as exc:
        run(_py(script) + [secret], lambda *a: None, redact=(secret,))
    assert secret not in str(exc.value) and exc.value.category == ErrorCategory.CREDENTIAL
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_a_hung_command_is_killed_at_the_timeout():
    with pytest.raises(GitSyncError) as exc:
        run(_py("import time; time.sleep(30)"), lambda *a: None, timeout=0.5)
    assert exc.value.category == ErrorCategory.TIMEOUT


def test_a_broken_observer_does_not_break_the_run():
    def boom(stage, pct):
        raise RuntimeError("observer bug")

    run(_py("import sys; sys.stderr.write('Receiving objects:  50% (1/2)\\n')"), boom)


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_a_real_clone_reports_progress(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "init.defaultBranch=main"]
    subprocess.run(git + ["init", "-q", str(src)], check=True)
    for i in range(40):
        (src / f"f{i}.txt").write_text("x" * 2000 + str(i))
    subprocess.run(git + ["-C", str(src), "add", "."], check=True)
    subprocess.run(git + ["-C", str(src), "commit", "-qm", "c"], check=True)
    seen = []
    run(["git", "clone", "--progress", "--depth", "1", f"file://{src}", str(tmp_path / "dst")],
        lambda stage, pct: seen.append((stage, pct)))
    assert ("receiving", 100.0) in seen
    assert (tmp_path / "dst" / "f0.txt").exists()
