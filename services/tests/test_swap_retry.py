"""adapters/mirror_sync/git_adapter._rename_with_retry — the mirror swap's renames survive a brief
Windows file lock (2026-10-12 06:25: two trees lost their swap to `[WinError 5] Access is denied`
while another process held a file inside them open)."""
from __future__ import annotations

import pytest

from adapters.mirror_sync.git_adapter import _rename_with_retry
from domain.errors import ErrorCategory, TransientError


class _Flaky:
    """A path whose rename fails with PermissionError `failures` times, then succeeds."""
    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0
        self.name = "main"

    def rename(self, dst):
        self.calls += 1
        if self.calls <= self.failures:
            raise PermissionError(5, "Access is denied")


def test_a_brief_lock_is_retried_until_the_rename_succeeds():
    src, waits = _Flaky(failures=2), []
    _rename_with_retry(src, "dst", delays=(0.5, 1, 2), sleep=waits.append)
    assert src.calls == 3 and waits == [0.5, 1]


def test_a_lock_that_outlasts_the_retries_raises_a_transient_error_naming_the_cause():
    src, waits = _Flaky(failures=99), []
    with pytest.raises(TransientError) as e:
        _rename_with_retry(src, "dst", delays=(0.5, 1), sleep=waits.append)
    assert src.calls == 3 and waits == [0.5, 1]
    assert e.value.category == ErrorCategory.TRANSIENT
    assert "held open by another process" in str(e.value)


def test_other_errors_are_not_retried(tmp_path):
    with pytest.raises(FileNotFoundError):
        _rename_with_retry(tmp_path / "missing", tmp_path / "dst", delays=(0.5,), sleep=lambda s: None)


def test_real_rename(tmp_path):
    (tmp_path / "staging").mkdir()
    _rename_with_retry(tmp_path / "staging", tmp_path / "active", sleep=lambda s: None)
    assert (tmp_path / "active").is_dir() and not (tmp_path / "staging").exists()
